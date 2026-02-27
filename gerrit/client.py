import requests
import logging
from requests.auth import HTTPBasicAuth
from urllib.parse import quote_plus, quote
import concurrent.futures
import json

logger = logging.getLogger(__name__)

class GerritRestClient:
    """Wrapper for Gerrit REST API interactions."""

    def __init__(self, base_url, username, password, timeout=30, max_workers=5):
        from requests.adapters import HTTPAdapter
        self.base_url = base_url.rstrip('/')
        self.auth = HTTPBasicAuth(username, password)
        self.max_workers = max_workers
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=max_workers, pool_maxsize=max_workers)
        self._session.mount('http://', adapter)
        self._session.mount('https://', adapter)
        self._session.auth = self.auth
        self.timeout = timeout

    def _strip_magic_prefix(self, response_text):
        """Gerrit prefixes JSON responses with a magic string to prevent CSRF.
        See: https://gerrit-review.googlesource.com/Documentation/rest-api.html#output
        """
        prefix = ")]}'\n"
        if response_text.startswith(prefix):
            return response_text[len(prefix):]
        return response_text

    def get_diffs(self, project, change_id, revision_id):
        """
        Fetches the patchset details, including the files changed and their diffs.
        Returns a dictionary mapping file paths to their diff content.
        """
        change_identifier = str(change_id)

        # 1. Fetch the list of files changed in this revision
        # GET /a/changes/{change-id}/revisions/{revision-id}/files/
        files_url = f"{self.base_url}/a/changes/{change_identifier}/revisions/{revision_id}/files/"
        try:
            rv = self._session.get(files_url, timeout=self.timeout)
            rv.raise_for_status()
            files_data = json.loads(self._strip_magic_prefix(rv.text))
        except Exception as e:
            logger.error(f"Failed to fetch files for change {change_identifier}: {e}")
            return {}

        diffs = {}
        
        IGNORE_EXTENSIONS = {
            '.png', '.jpg', '.jpeg', '.gif', '.ico', '.pdf', '.zip', '.tar', '.gz', 
            '.pyc', '.class', '.exe', '.dll', '.so', '.dylib', '.woff', '.woff2', '.ttf'
        }
        IGNORE_FILES = {
            'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 
            'poetry.lock', 'Cargo.lock', 'go.sum', 'Gemfile.lock'
        }

        filenames = []
        for f in files_data.keys():
            if f == "/COMMIT_MSG":
                continue
            if any(f.endswith(ext) for ext in IGNORE_EXTENSIONS):
                logger.debug(f"Skipping binary/ignored extension: {f}")
                continue
            if f.split('/')[-1] in IGNORE_FILES:
                logger.debug(f"Skipping generated lockfile: {f}")
                continue
            filenames.append(f)

        def fetch_single_diff(filename):
            # Encode filename carefully for Gerrit REST API
            encoded_filename = quote(filename, safe="")

            # 2. Fetch the diff for each file
            # GET /a/changes/{change-id}/revisions/{revision-id}/files/{file-id}/diff
            diff_url = f"{self.base_url}/a/changes/{change_identifier}/revisions/{revision_id}/files/{encoded_filename}/diff"
            try:
                drv = self._session.get(diff_url, timeout=self.timeout)
                drv.raise_for_status()
                diff_data = json.loads(self._strip_magic_prefix(drv.text))
                
                # Format the diff content to be LLM readable.
                return filename, self._format_diff(diff_data)
            except Exception as e:
                logger.error(f"Failed to fetch diff for file {filename}: {e}")
                return filename, None

        # Fetch in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_file = {executor.submit(fetch_single_diff, f): f for f in filenames}
            for future in concurrent.futures.as_completed(future_to_file):
                filename, diff_content = future.result()
                if diff_content:
                    diffs[filename] = diff_content
        
        return diffs

    def is_latest_patchset(self, change_id, revision_id):
        """
        Checks if the provided revision_id is the current/latest patchset for the change.
        """
        change_identifier = str(change_id)
        url = f"{self.base_url}/a/changes/{change_identifier}?o=CURRENT_REVISION"
        try:
            rv = self._session.get(url, timeout=self.timeout)
            rv.raise_for_status()
            data = json.loads(self._strip_magic_prefix(rv.text))
            return data.get("current_revision") == revision_id
        except Exception as e:
            logger.error(f"Failed to fetch change details for {change_identifier}: {e}")
            return False

    def post_review(self, project, change_id, revision_id, message, comments=None, code_review_vote=0):
        """
        Posts a review to the specified patchset.

        Args:
            project (str): Project name
            change_id (str): Change ID (e.g. Ixxxx...) or change number
            revision_id (str): Revision ID (commit hash) or patchset number
            message (str): Overall review message.
            comments (dict): Inline comments. Map of filename string to list of Comment input dicts.
            code_review_vote (int): Vote for the "Code-Review" label (-2, -1, 0, 1, 2)
        """
        change_identifier = str(change_id)
        review_url = f"{self.base_url}/a/changes/{change_identifier}/revisions/{revision_id}/review"

        payload = {
            "message": message,
            "labels": {
                "Code-Review": code_review_vote
            }
        }
        
        if comments:
            payload["comments"] = comments

        logger.info(f"Posting review to {change_identifier}, vote={code_review_vote}")
        try:
            rv = self._session.post(review_url, json=payload, timeout=self.timeout)
            rv.raise_for_status()
            return True
        except requests.exceptions.HTTPError as e:
            logger.error(f"Failed to post review. HTTP Error: {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Failed to post review: {e}")
            return False

    def _format_diff(self, diff_data):
        """Helper to convert Gerrit diff JSON into unified-diff like string with line numbers."""
        formatted = []
        if 'diff_header' in diff_data:
            formatted.append("\n".join(diff_data['diff_header']))
        
        if 'content' not in diff_data:
            return "\n".join(formatted)

        line_a = 1
        line_b = 1

        for chunk in diff_data['content']:
            if 'skip' in chunk:
                # 'skip' provides the number of skipped lines that are common to both sides
                skip_count = chunk['skip']
                line_a += skip_count
                line_b += skip_count
                formatted.append(f"... skipped {skip_count} lines ...")
            elif 'ab' in chunk or 'a' in chunk or 'b' in chunk:
                # Calculate lengths for unified diff header
                len_a = len(chunk.get('ab', [])) + len(chunk.get('a', []))
                len_b = len(chunk.get('ab', [])) + len(chunk.get('b', []))
                formatted.append(f"@@ -{line_a},{len_a} +{line_b},{len_b} @@")

                if 'ab' in chunk: # lines that are common to both sides
                    for line in chunk['ab']:
                        formatted.append(f" {line_b:4d} |  {line}")
                        line_a += 1
                        line_b += 1

                if 'a' in chunk: # lines present in 'a' but removed in 'b'
                    for line in chunk['a']:
                        formatted.append(f"      | -{line}")
                        line_a += 1

                if 'b' in chunk: # lines added to 'b'
                    for line in chunk['b']:
                        formatted.append(f" {line_b:4d} | +{line}")
                        line_b += 1

        return "\n".join(formatted)

    def remove_reviewer(self, project, change_id, account_id):
        """
        Removes a reviewer from a change.

        Args:
            project (str): Project name
            change_id (str): Change ID (e.g. Ixxxx...) or change number
            account_id (str): Account ID or username of the reviewer to remove
        """
        change_identifier = str(change_id)
        encoded_account = quote_plus(str(account_id))
        
        url = f"{self.base_url}/a/changes/{change_identifier}/reviewers/{encoded_account}"

        logger.info(f"Removing reviewer {account_id} from {change_identifier}")
        try:
            rv = self._session.delete(url, timeout=self.timeout)
            rv.raise_for_status()
            return True
        except requests.exceptions.HTTPError as e:
            logger.error(f"Failed to remove reviewer. HTTP Error: {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Failed to remove reviewer: {e}")
            return False
