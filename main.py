import os
import sys
import logging
from dotenv import load_dotenv

from gerrit.stream import GerritStreamListener
from gerrit.client import GerritRestClient
from analyzer.analyzer import LiteLLMAnalyzer
from bot.handler import ReviewHandler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(filename)s:%(lineno)d - %(levelname)s - %(message)s'
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("litellm").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

def main():
    load_dotenv()

    # Gerrit connection settings
    gerrit_ssh_host = os.getenv("GERRIT_SSH_HOST", "localhost")
    try:
        gerrit_ssh_port = int(os.getenv("GERRIT_SSH_PORT", "29418"))
    except ValueError:
        logger.warning("Invalid GERRIT_SSH_PORT provided. Defaulting to 29418.")
        gerrit_ssh_port = 29418
    gerrit_rest_url = os.getenv("GERRIT_REST_URL", "http://localhost:8080")
    gerrit_username = os.getenv("GERRIT_USERNAME")
    gerrit_ssh_key_path = os.getenv("GERRIT_SSH_KEY_PATH")
    gerrit_ssh_host_key = os.getenv("GERRIT_SSH_HOST_KEY")
    gerrit_http_password = os.getenv("GERRIT_HTTP_PASSWORD")
    
    # LiteLLM settings
    # For litellm proxy, we just need the api_base. The model name dictates the provider.
    litellm_proxy_url = os.getenv("LITELLM_PROXY_URL")
    # The default model string to pass to litigation proxy (e.g. gpt-4, claude-3-opus, gemini-pro)
    llm_model = os.getenv("LLM_MODEL")
    litellm_api_key = os.getenv("LITELLM_MASTER_KEY")
    
    try:
        llm_temperature = float(os.getenv("LLM_TEMPERATURE", "0.2"))
    except ValueError:
        logger.warning("Invalid LLM_TEMPERATURE provided. Defaulting to 0.2.")
        llm_temperature = 0.2
        
    try:
        max_workers = int(os.getenv("MAX_WORKERS", "5"))
    except ValueError:
        logger.warning("Invalid MAX_WORKERS provided. Defaulting to 5.")
        max_workers = 5
    
    if not all([gerrit_ssh_host, gerrit_ssh_port]):
        logger.error("Missing required environment variables: GERRIT_SSH_HOST or GERRIT_SSH_PORT")
        sys.exit(1)

    if not all([gerrit_username, gerrit_ssh_key_path, gerrit_http_password]):
        logger.error("Missing required environment variables: GERRIT_USERNAME, GERRIT_SSH_KEY_PATH, or GERRIT_HTTP_PASSWORD")
        sys.exit(1)

    if not all([litellm_proxy_url, llm_model]):
        logger.error("Missing required environment variables: LITELLM_PROXY_URL or LLM_MODEL")
        sys.exit(1)

    logger.info(f"Starting Gerrit Review Bot '{gerrit_username}'")
    logger.info(f"Connecting to Gerrit SSH at {gerrit_ssh_host}:{gerrit_ssh_port}")
    logger.info(f"Connecting to Gerrit REST API at {gerrit_rest_url}")
    logger.info(f"Using LiteLLM Proxy at {litellm_proxy_url} with model {llm_model}")

    # Initialize components
    rest_client = GerritRestClient(
        base_url=gerrit_rest_url,
        username=gerrit_username,
        password=gerrit_http_password,
        max_workers=max_workers,
    )

    analyzer = LiteLLMAnalyzer(
        api_base=litellm_proxy_url,
        model=llm_model,
        api_key=litellm_api_key,
        temperature=llm_temperature
    )

    remove_bot_reviewer = os.getenv("REMOVE_BOT_REVIEWER", "False").lower() in ("true", "1", "yes")
    verify_ssh_host = os.getenv("VERIFY_SSH_HOST", "True").lower() in ("true", "1", "yes")

    handler = ReviewHandler(
        bot_username=gerrit_username,
        rest_client=rest_client,
        analyzer=analyzer,
        remove_after_review=remove_bot_reviewer
    )

    stream_listener = GerritStreamListener(
        host=gerrit_ssh_host,
        port=gerrit_ssh_port,
        username=gerrit_username,
        key_filename=gerrit_ssh_key_path,
        host_key=gerrit_ssh_host_key,
        event_handler=handler.handle_event,
        verify_host_key=verify_ssh_host,
        max_workers=max_workers
    )

    try:
        # Blocks and listens to the stream forever
        stream_listener.start_listening()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
