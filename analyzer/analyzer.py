import json
import logging
import requests
import re

logger = logging.getLogger(__name__)

class LiteLLMAnalyzer:
    """Analyzes Gerrit diffs using an LLM via LiteLLM."""

    def __init__(self, api_base, model, api_key=None, temperature=0.2):
        self.api_base = api_base
        self.model = model
        self.api_key = api_key
        self.temperature = temperature

    def analyze(self, diffs):
        """
        Takes a dict of filename -> diff string, and returns a review message 
        and optional inline comments.
        
        Returns:
            tuple: (message: str, comments: dict, vote: int)
        """
        if not diffs:
            return "No valid diffs found to review.", None, 0

        # Construct a prompt for the LLM
        prompt = self._build_prompt(diffs)
        
        logger.info(f"Sending prompt to LLM: {self.model} at {self.api_base}")

        try:
            # Let litellm handle provider routing
            target_model = self.model

            system_prompt = """
I am an automated code review bot analyzing Gerrit diffs. My task is to meticulously analyze the provided code diff and offer insightful, actionable feedback. Focus on:
1.  **Potential Bugs:** Identify logical errors, edge cases, race conditions, security vulnerabilities (e.g., XSS, SQLi), etc.
2.  **Best Practices & Design Patterns:** Suggest improvements based on established software engineering principles (SOLID, DRY, KISS) and relevant design patterns.
3.  **Readability & Maintainability:** Comment on code clarity, naming conventions (e.g., camelCase for variables/functions, PascalCase for classes), complexity (e.g., Cyclomatic complexity), and opportunities for simplification or refactoring. Mention magic numbers or hardcoded strings if they appear.
4.  **Performance:** Highlight any potential performance bottlenecks (e.g., inefficient loops, unnecessary computations) or suggest optimizations.
5.  **Testability:** Comment on how easy or difficult the code would be to test (e.g., presence of side effects, tight coupling) and suggest improvements for better testability.
6.  **Style Guide Adherence (General):** Point out common style issues (e.g., inconsistent indentation, mixed quotes). Assume a generally accepted style guide like Google's JavaScript Style Guide or Python's PEP 8 if the language is identifiable.
7.  **Security Considerations:** If applicable, point out any security flaws or areas that need hardening.
8.  **Clarity of Comments and Documentation:** Assess if comments are helpful, or if code needs more comments or better docstrings.
"""
            
            # Call via requests to get exact proxy headers (for x-litellm-response-cost)
            endpoint = f"{self.api_base.rstrip('/')}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
            payload = {
                "model": target_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "response_format": {"type": "json_object"},
                "temperature": self.temperature
            }

            resp = requests.post(endpoint, headers=headers, json=payload, timeout=300)
            
            if resp.status_code == 401:
                return f"Authentication Error: Please check your LLM provider API keys.", {}, 0
            elif resp.status_code == 429:
                return f"Rate Limit Exceeded: The LLM provider rejected the request due to quota limits.", {}, 0
            
            resp.raise_for_status()
            response_json = resp.json()

            result_content = response_json.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.debug(f"LLM Raw Response: {result_content}")

            message, gerrit_comments, vote = self._parse_llm_response(result_content)
            
            # Append token usage and estimated cost to the summary message
            try:
                # Safely extract usage stats from the JSON payload
                usage = response_json.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", 0)
                
                # Extract cost tracked directly by the proxy
                cost_header = resp.headers.get("x-litellm-response-cost")
                cost = float(cost_header) if cost_header else 0.0
                
                # Grab the final model string the proxy executed.
                # LiteLLM proxy returns x-litellm-model-api-base if it's forwarding to a provider
                api_base_header = resp.headers.get("x-litellm-model-api-base", "")
                
                # Try to extract a clean model name or fallback to the JSON model alias
                if api_base_header and 'models/' in api_base_header:
                    # For providers like gemini
                    final_model = api_base_header.split('models/')[-1].split(':')[0]
                elif api_base_header and 'openai' not in api_base_header.lower():
                    # For generically patterned URL endpoints
                    final_model = f"{response_json.get('model', self.model)} (via {api_base_header.split('/')[2]})"
                else:
                    final_model = response_json.get("model", self.model)
                
                stats_msg = (
                    f"\n\n---\n**LLM Usage Stats:**\n"
                    f"* Model: {final_model}\n"
                    f"* Input Tokens: {prompt_tokens}\n"
                    f"* Output Tokens: {completion_tokens}\n"
                    f"* Total Tokens: {total_tokens}\n"
                    f"* Estimated Cost: ${cost:.6f}"
                )
                message += stats_msg
            except Exception as e:
                logger.warning(f"Could not extract token usage or cost: {e}")

            return message, gerrit_comments, vote

        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP Request Error calling LiteLLM Proxy: {e}")
            return f"LLM API Error: Could not reach the LiteLLM Proxy. Details: {e}", {}, 0
        except Exception as e:
            logger.error(f"Unexpected error calling LiteLLM Proxy: {e}")
            return f"An error occurred during automated code review: {e}", {}, 0

    def _build_prompt(self, diffs):
        prompt = "Review the following code changes:\n\n"
        for idx, (filename, diff) in enumerate(diffs.items()):
            prompt += f"--- File: {filename} ---\n```\n{diff}\n```\n\n"
        
        prompt += """
Please provide your review in the following EXACT JSON format:
{
  "summary": "Overall summary of the changes and your assessment.",
  "vote": <int>,
  "comments": {
    "filename/with/path.py": [
      {
        "line": <int>,
        "message": "Inline comment for this specific modified or added line."
      }
    ]
  }
}

Rules:
- The `vote` field must be an integer: +1 (looks good), 0 (neutral), or -1 (issues found). Do not vote +2 or -2.
- The `comments` dictionary should have filenames exactly as provided above as keys.
- Inside the array for each filename, the `line` must be the line number in the unified diff context that you are commenting on. If you cannot determine the line number, omit the inline comment and put the feedback in the summary.
- You CANNOT post inline comments on removed lines (lines starting with '-'). Only post inline comments for added or unchanged lines (lines with a line number).
- Omit markdown syntax, backticks, or other formatting around the JSON string. Output ONLY valid JSON.
- If the diff is empty, trivial, or contains no significant code changes (e.g., only comments or whitespace changes), state that clearly.
- Be specific in your suggestions. Instead of saying "this could be better", explain *how* it could be better and suggest a specific change.
- If you identify a critical issue, please flag it as such. Only vote -1 if there are critical issues.
- Only comment on areas that really need improvement. Do not comment on things that are just minor or cosmetic. If there is no notable area to comment on, just give a summary for the code review and +1 vote.
- Zero Fluff: No philosophical lectures or unsolicited advice.
- Stay Focused: Concise answers only. No wandering.
"""
        return prompt

    def _parse_llm_response(self, raw_content):
        # Use regex to robustly extract JSON block if models output markdown tags despite instructions
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_content, re.DOTALL)
        if json_match:
            clean_content = json_match.group(1).strip()
        else:
            # Fallback to just stripping the string if no markdown block is found
            clean_content = raw_content.strip()

        try:
            parsed = json.loads(clean_content)
            
            message = parsed.get("summary", "Automated code review completed.")
            vote = parsed.get("vote", 0)
            
            # Ensure vote is within bound -1 to 1 based on our bot permissions
            if vote not in [-1, 0, 1]:
                 logger.warning(f"Invalid vote {vote} returned from LLM. Defaulting to 0.")
                 vote = 0

            # Convert to Gerrit's inline comment format
            # The REST API format for posting inline comments is a dict mapping filename -> list of comment objects
            # Format: {"tests/test_something.py": [{"line": 10, "message": "Typo here"}]}
            gerrit_comments = parsed.get("comments", {})

            return message, gerrit_comments, vote

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON. Error: {e}")
            logger.error(f"Raw response was: {raw_content}")
            return "Failed to parse the automated review results.", {}, -1
