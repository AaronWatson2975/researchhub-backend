import datetime

from django.core.management.base import BaseCommand

from discussion.reaction_models import Vote as GrmVote
from paper.models import Paper


class Command(BaseCommand):
    def handle(self, *args, **options):
        today = datetime.datetime.now()
        paper_sync_stop_date = datetime.datetime(
            year=2019, month=1, day=1, hour=0, minute=0, second=0
        )
        papers = Paper.objects.filter(
            created_date__lte=today,
            created_date__gte=paper_sync_stop_date,
            unified_document__isnull=True,
        ).order_by("created_date")
        count = papers.count()
        for i, paper in enumerate(papers.iterator()):
            print(f"{i + 1}/{count} - paper_id {paper.id}")
            legacy_votes = paper.votes_legacy
            for j, vote_legacy in enumerate(legacy_votes.iterator()):
                try:
                    grm_vote = GrmVote(
                        created_by=vote_legacy.created_by,
                        created_date=vote_legacy.created_date,
                        item=paper,
                        updated_date=vote_legacy.created_date,
                        vote_type=vote_legacy.vote_type,
                    )
                    grm_vote.save()
                except Exception as exception:
                    print("ERROR (backfill_paper_to_grm): ", exception)
