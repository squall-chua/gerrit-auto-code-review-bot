import logging

logger = logging.getLogger(__name__)

class ReviewHandler:
    """Orchestrates the reception of events, fetching of diffs, analysis, and posting of reviews."""

    def __init__(self, bot_username, rest_client, analyzer, remove_after_review=False):
        self.bot_username = bot_username
        self.rest_client = rest_client
        self.analyzer = analyzer
        self.remove_after_review = remove_after_review

    def handle_event(self, event):
        """Called by the GerritStreamListener when a 'reviewer-added' event occurs."""

        # 1. Validate if the bot was the one added
        reviewer = event.get('reviewer', {})
        reviewer_username = reviewer.get('username')

        if reviewer_username != self.bot_username:
            logger.debug(f"Ignoring event: Reviewer added was '{reviewer_username}', not '{self.bot_username}'")
            return

        change = event.get('change', {})
        patchset = event.get('patchSet', {})

        project = change.get('project')
        # Use the project-scoped change ID (project~number) to prevent collisions across multiple repositories
        from urllib.parse import quote_plus
        safe_project = quote_plus(project) if project else ''
        change_num = change.get('number')
        change_id = f"{safe_project}~{change_num}"
        revision_id = patchset.get('revision')
        patchset_num = patchset.get('number')

        logger.info(f"Bot {self.bot_username} added as reviewer to {project}~{change_id} PS{patchset_num}")

        # Notify Gerrit that the review has started
        self.rest_client.post_review(
            project=project,
            change_id=change_id,
            revision_id=revision_id,
            message="Starting automated code review...",
            code_review_vote=0
        )

        # 2. Fetch the diffs for this patchset
        diffs = self.rest_client.get_diffs(project, change_id, revision_id)

        if not diffs:
            logger.warning(f"No diffs found for {change_id} PS{patchset_num}. Skipping review.")
            return
            
        logger.info(f"Fetched diffs for {len(diffs)} files. Starting analysis.")

        # 3. Analyze the diffs
        # message: str, comments: dict, vote: int
        message, comments, vote = self.analyzer.analyze(diffs)

        # Verify the patchset hasn't been superseded while the LLM was processing
        if not self.rest_client.is_latest_patchset(change_id, revision_id):
            logger.warning(f"Change {change_id} PS{patchset_num} was superseded. Discarding review.")
            return

        # 4. Post the review back to Gerrit
        success = self.rest_client.post_review(
            project=project,
            change_id=change_id,
            revision_id=revision_id,
            message=message,
            comments=comments,
            code_review_vote=vote
        )

        if success:
            logger.info(f"Successfully posted review for {change_id} PS{patchset_num} with vote {vote}")
            
            # 5. Remove bot from the reviewer list (optional)
            if self.remove_after_review:
                remove_success = self.rest_client.remove_reviewer(project, change_id, self.bot_username)
                if remove_success:
                    logger.info(f"Successfully removed {self.bot_username} from reviewers on {change_id}")
                else:
                    logger.warning(f"Failed to remove {self.bot_username} from reviewers on {change_id}")
            else:
                logger.info(f"Bot remains as reviewer on {change_id} as per configuration.")
        else:
            logger.error(f"Failed to post review for {change_id} PS{patchset_num}")
