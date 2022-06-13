from django.db.models.signals import post_save
from django.dispatch import receiver

from discussion.reaction_models import Vote as GrmVote
from paper.models import Paper
from researchhub_document.tasks import recalc_hot_score_task
from utils import sentry


@receiver(post_save, sender=GrmVote, dispatch_uid="recalc_hot_score_on_vote")
def recalc_hot_score(instance, sender, **kwargs):
    try:
        recalc_hot_score_task.apply_async(
            (
                instance.content_type_id,
                instance.object_id,
            ),
            priority=2,
            countdown=5,
        )
        # TODO: calvinhlee - remove this if works on staging.
        # elif isinstance(instance, ResearchhubUnifiedDocument):
        #     inner_doc = instance.get_document()
        #     content_type_id = ContentType.objects.get_for_model(inner_doc).id

        #     recalc_hot_score_task.apply_async(
        #         (content_type_id, inner_doc.id), priority=2, countdown=5
        #     )
    except Exception as error:
        print("recalc_hot_score error", error)
        sentry.log_error(error)


@receiver(
    post_save,
    sender=Paper,
    dispatch_uid="sync_is_removed_from_paper",
)
def sync_is_removed_from_paper(instance, **kwargs):
    try:
        uni_doc = instance.unified_document
        if uni_doc is not None:
            uni_doc.is_removed = instance.is_removed
            uni_doc.save()
    except Exception:
        return None


@receiver(
    post_save,
    sender=GrmVote,
    dispatch_uid="rh_unified_doc_sync_scores_vote",
)
def rh_unified_doc_sync_scores_on_related_docs(instance, sender, **kwargs):
    if not isinstance(instance, (GrmVote)):
        return

    unified_document = instance.unified_document
    if not unified_document:
        return

    document_obj = instance.item

    sync_scores(unified_document, document_obj)


def sync_scores(unified_doc, document_obj):
    should_save = False
    score = document_obj.calculate_score()  # refer to AbstractGenericReactionModel
    hot_score = document_obj.calculate_hot_score()  # AbstractGenericReactionModel
    if unified_doc.hot_score != hot_score:
        unified_doc.hot_score = hot_score
        should_save = True
    if unified_doc.score != score:
        unified_doc.score = score
        should_save = True
    if should_save:
        unified_doc.save()
