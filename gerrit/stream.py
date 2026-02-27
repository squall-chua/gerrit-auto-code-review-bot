import json
import logging
import concurrent.futures
import time
import base64
import socket
import paramiko
import threading

logger = logging.getLogger(__name__)

class GerritStreamListener:
    def __init__(self, host, port, username, key_filename, event_handler, host_key=None, verify_host_key=True, max_workers=5):
        """
        Initializes the Gerrit stream listener.

        Args:
            host (str): Gerrit SSH host.
            port (int): Gerrit SSH port.
            username (str): Bot username.
            key_filename (str): Path to the SSH private key.
            host_key (str): Optional base64-encoded ED25519 known host key for the Gerrit server.
            event_handler (callable): Function to call with parsed event payload.
            verify_host_key (bool): Whether to enforce SSH host key verification.
            max_workers (int): Maximum number of concurrent event processing threads.
        """
        self.host = host
        self.port = port
        self.username = username
        self.key_filename = key_filename
        self.host_key = host_key
        self.event_handler = event_handler
        self.verify_host_key = verify_host_key
        self._running = False
        self._ssh_client = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self._active_reviews = set()
        self._lock = threading.Lock()

    def connect(self):
        """Establishes an SSH connection to Gerrit."""
        self._ssh_client = paramiko.SSHClient()
        self._ssh_client.load_system_host_keys()

        if self.host_key:
            # The host_key string could be a raw base64 string, or a full known_hosts line (e.g. "ssh-rsa AAA...")
            parts = self.host_key.strip().split()
            key_data_b64 = parts[1] if len(parts) > 1 else parts[0]
            
            try:
                key_bytes = base64.b64decode(key_data_b64)
                parsed_key = None
                
                # Attempt to decode using available paramiko key classes
                for key_class in [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey, paramiko.DSSKey]:
                    try:
                        parsed_key = key_class(data=key_bytes)
                        break
                    except Exception:
                        continue
                
                if parsed_key:
                    key_name = parsed_key.get_name()
                    self._ssh_client.get_host_keys().add(self.host, key_name, parsed_key)
                    # Also add for specific port if not using default 22
                    self._ssh_client.get_host_keys().add(f"[{self.host}]:{self.port}", key_name, parsed_key)
                    logger.info(f"Successfully loaded provided {key_name} host key.")
                else:
                    logger.warning("Failed to decode provided host key into any known SSH format (Ed25519, ECDSA, RSA, DSS).")

            except Exception as e:
                logger.warning(f"Error parsing provided host key: {e}")

        if self.verify_host_key:
            self._ssh_client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            logger.warning("SSH Host Key Verification is DISABLED! The bot is vulnerable to MitM attacks.")
            self._ssh_client.set_missing_host_key_policy(paramiko.WarningPolicy())

        try:
            logger.info(f"Connecting to SSH at {self.username}@{self.host}:{self.port}")
            self._ssh_client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                key_filename=self.key_filename,
                look_for_keys=False,
                allow_agent=False
            )
            logger.info("SSH connection established successfully.")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Gerrit SSH: {e}")
            return False

    def start_listening(self):
        """Starts listening to 'gerrit stream-events'."""
        self._running = True
        base_retry_delay = 2
        max_retry_delay = 60
        retry_delay = base_retry_delay

        while self._running:
            if not self.connect():
                logger.info(f"Retrying connection in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)
                continue

            # Connection successful, reset backoff
            retry_delay = base_retry_delay
            stdin = stdout = stderr = None
            try:
                # Execute stream-events command
                # We use exec_command which returns stdin, stdout, stderr
                stdin, stdout, stderr = self._ssh_client.exec_command('gerrit stream-events')

                logger.info("Listening to Gerrit stream-events...")

                # stdout is an iterator of lines
                for line in stdout:
                    if not self._running:
                        break

                    line = line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                        self._process_event(event)
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to decode JSON event: {e}. Raw line: {line}")
                    except Exception as e:
                        logger.error(f"Error processing event: {e}", exc_info=True)

            except (socket.timeout, TimeoutError):
                # This is normal since we enforce a 60s timeout on the exec_command socket reading
                logger.debug("Stream socket timeout reached. Reconnecting to keep underlying TCP session alive...")
            except paramiko.SSHException as e:
                logger.error(f"SSH Exception occurred: {e}. Reconnecting...")
            except Exception as e:
                # Log the actual class name to make debugging easier for bare exceptions
                logger.error(f"Unexpected error in stream loop: {e.__class__.__name__}({e}). Reconnecting...")
            finally:
                # Explicitly close streams to prevent dangling resources on the server
                if stdin:
                    stdin.close()
                if stdout:
                    stdout.close()
                if stderr:
                    stderr.close()
                
                if self._ssh_client:
                    self._ssh_client.close()

            if self._running:
                logger.info(f"Reconnecting in {retry_delay} seconds...")
                time.sleep(retry_delay)

    def stop(self):
        """Stops the listener."""
        self._running = False
        if self._ssh_client:
            self._ssh_client.close()
        self._executor.shutdown(wait=False)
        logger.info("Stream listener stopped.")

    def _process_event(self, event):
        """Filters and routes the event."""
        # Check for stale events to prevent processing backlog if the bot reconnects
        event_created_on = event.get('eventCreatedOn')
        if event_created_on:
            age = time.time() - event_created_on
            if age > 300: # Discard events older than 5 minutes
                logger.debug(f"Discarding stale event ({age:.1f}s old).")
                return

        event_type = event.get('type')
        if event_type == 'reviewer-added':
            change_num = event.get('change', {}).get('number')
            patchset_num = event.get('patchSet', {}).get('number')
            # Create a unique identifier for this specific patchset review
            review_id = f"{change_num}-{patchset_num}"

            with self._lock:
                if review_id in self._active_reviews:
                    logger.debug(f"Skipping duplicate reviewer-added event for change {change_num} PS {patchset_num}.")
                    return

                logger.debug(f"Received reviewer-added event for change {change_num} PS {patchset_num}")
                self._active_reviews.add(review_id)

            def finalize_event(f, rid=review_id):
                with self._lock:
                    self._active_reviews.discard(rid)

            # Run the handler in the thread pool so we don't block reading from the stream
            future = self._executor.submit(self.event_handler, event)
            future.add_done_callback(finalize_event)
