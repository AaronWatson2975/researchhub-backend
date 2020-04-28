import datetime

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db.models import (
    Count,
    Q,
    Prefetch,
    F,
    Avg,
    Max,
    Min,
    IntegerField
)
from django.db.models.functions import Extract, Now
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from elasticsearch.exceptions import ConnectionError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.permissions import (
    IsAuthenticatedOrReadOnly,
    IsAuthenticated
)
from rest_framework.response import Response

from bullet_point.models import BulletPoint
from paper.exceptions import PaperSerializerError
from paper.filters import PaperFilter
from paper.models import Figure, Flag, Paper, Vote
from paper.permissions import (
    CreatePaper,
    FlagPaper,
    IsAuthor,
    IsModeratorOrVerifiedAuthor,
    UpdatePaper,
    UpvotePaper,
    DownvotePaper
)
from paper.serializers import (
    BookmarkSerializer,
    HubPaperSerializer,
    FlagSerializer,
    FigureSerializer,
    PaperSerializer,
    PaperReferenceSerializer,
    PaperVoteSerializer,
)
from paper.utils import (
    get_csl_item,
    get_pdf_location_for_csl_item,
    get_cache_key,
    invalidate_trending_cache,
    invalidate_top_rated_cache,
    invalidate_newest_cache,
    invalidate_most_discussed_cache,
)
from utils.http import GET, POST, check_url_contains_pdf
from utils.sentry import log_error


class PaperViewSet(viewsets.ModelViewSet):
    queryset = Paper.objects.all()
    serializer_class = PaperSerializer
    filter_backends = (SearchFilter, DjangoFilterBackend, OrderingFilter)
    search_fields = ('title', 'doi')
    filter_class = PaperFilter
    ordering = ('-uploaded_date')

    permission_classes = [
        IsAuthenticatedOrReadOnly
        & CreatePaper
        & UpdatePaper
    ]

    def prefetch_lookups(self):
        return (
            'uploaded_by',
            'uploaded_by__bookmarks',
            'uploaded_by__author_profile',
            'uploaded_by__author_profile__user',
            'uploaded_by__subscribed_hubs',
            'authors',
            'authors__user',
            Prefetch(
                'bullet_points',
                queryset=BulletPoint.objects.filter(
                    is_head=True
                )
            ),
            'summary',
            'summary__previous',
            'summary__proposed_by__bookmarks',
            'summary__proposed_by__subscribed_hubs',
            'summary__proposed_by__author_profile',
            'summary__paper',
            'moderators',
            'hubs',
            'hubs__subscribers',
            'votes',
            'flags',
            'threads',
            'threads__comments',
            Prefetch(
                'figures',
                queryset=Figure.objects.filter(
                    figure_type=Figure.FIGURE
                ),
                to_attr='figure_list',
            ),
            Prefetch(
                'figures',
                queryset=Figure.objects.filter(
                    figure_type=Figure.PREVIEW
                ),
                to_attr='preview_list',
            ),
            Prefetch(
                'votes',
                queryset=Vote.objects.filter(
                    created_by=self.request.user.id,
                ),
                to_attr='vote_created_by',
            ),
            Prefetch(
                'flags',
                queryset=Flag.objects.filter(
                    created_by=self.request.user.id,
                ),
                to_attr='flag_created_by',
            ),
        )

    def get_queryset(self, prefetch=True):
        query = Q(is_public=True)
        query_params = self.request.query_params
        if query_params.get('make_public') or query_params.get('all'):
            query = Q()

        user = self.request.user
        if user.is_staff:
            return self.queryset
        if prefetch:
            return self.queryset.filter(query).prefetch_related(
                *self.prefetch_lookups()
            )
        else:
            return self.queryset.filter(query)

    def create(self, *args, **kwargs):
        try:
            response = super().create(*args, **kwargs)
            request = args[0]
            hub_ids = list(request.POST['hubs'])
            invalidate_trending_cache(hub_ids)
            invalidate_newest_cache(hub_ids)
            return response
        except PaperSerializerError as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, *args, **kwargs):
        cache_key = get_cache_key(request, 'paper')
        cache_hit = cache.get(cache_key)
        instance = self.get_object()
        if cache_hit is not None:
            vote = self.serializer_class(
                context={'request': request}
            ).get_user_vote(instance)
            cache_hit['user_vote'] = vote
            return Response(cache_hit)

        if request.query_params.get('make_public'):
            instance.is_public = True
            instance.save()
        serializer = self.get_serializer(instance)
        serializer_data = serializer.data

        cache.set(cache_key, serializer_data, timeout=60*60*24*7)
        return Response(serializer_data)

    @action(
        detail=True,
        methods=['put', 'patch', 'delete'],
        permission_classes=[IsAuthenticated, IsModeratorOrVerifiedAuthor]
    )
    def censor(self, request, pk=None):
        paper = self.get_object()
        paper.delete()
        return Response('Paper was deleted.', status=200)

    @action(
        detail=True,
        methods=['put', 'patch', 'delete'],
        permission_classes=[IsAuthenticated, IsModeratorOrVerifiedAuthor]
    )
    def censor_pdf(self, request, pk=None):
        paper = self.get_object()
        paper.file = None
        paper.url = ''
        paper.save()
        return Response(
            self.get_serializer(instance=paper).data,
            status=200
        )

    @action(
        detail=True,
        methods=['post', 'put', 'patch'],
        permission_classes=[IsAuthor]
    )
    def assign_moderator(self, request, pk=None):
        '''Assign users as paper moderators'''
        paper = self.get_object()
        moderators = request.data.get('moderators')
        if not isinstance(moderators, list):
            moderators = [moderators]
        paper.moderators.add(*moderators)
        paper.save()
        return Response(PaperSerializer(paper).data)

    @action(
        detail=True,
        methods=['post'],
        permission_classes=[IsAuthenticatedOrReadOnly]
    )
    def bookmark(self, request, pk=None):
        paper = self.get_object()
        user = request.user

        if paper in user.bookmarks.all():
            return Response('Bookmark already added', status=400)
        else:
            user.bookmarks.add(paper)
            user.save()
            serialized = BookmarkSerializer({
                'user': user.id,
                'bookmarks': user.bookmarks.all()
            })
            return Response(serialized.data, status=201)

    @bookmark.mapping.delete
    def delete_bookmark(self, request, pk=None):
        paper = self.get_object()
        user = request.user

        try:
            user.bookmarks.remove(paper)
            user.save()
            return Response(paper.id, status=200)
        except Exception as e:
            print(e)
            return Response(
                f'Failed to remove {paper.id} from bookmarks',
                status=400
            )

    @action(
        detail=True,
        methods=['post'],
        permission_classes=[FlagPaper]  # Also applies to delete_flag below
    )
    def flag(self, request, pk=None):
        paper = self.get_object()
        reason = request.data.get('reason')
        flag = Flag.objects.create(
            paper=paper,
            created_by=request.user,
            reason=reason
        )
        return Response(FlagSerializer(flag).data, status=201)

    @flag.mapping.delete
    def delete_flag(self, request, pk=None):
        try:
            flag = Flag.objects.get(
                paper=pk,
                created_by=request.user.id
            )
            flag_id = flag.id
            flag.delete()
            return Response(flag_id, status=200)
        except Exception as e:
            return Response(f'Failed to delete flag: {e}', status=400)

    @action(detail=True, methods=[GET])
    def referenced_by(self, request, pk=None):
        paper = self.get_object()
        queryset = paper.referenced_by.all()
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = PaperReferenceSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=[GET])
    def references(self, request, pk=None):
        paper = self.get_object()
        queryset = paper.references.all()
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = PaperReferenceSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'])
    def user_vote(self, request, pk=None):
        paper = self.get_object()
        user = request.user
        vote = retrieve_vote(user, paper)
        return get_vote_response(vote, 200)

    @user_vote.mapping.delete
    def delete_user_vote(self, request, pk=None):
        try:
            paper = self.get_object()
            user = request.user
            vote = retrieve_vote(user, paper)
            vote_id = vote.id
            vote.delete()
            return Response(vote_id, status=200)
        except Exception as e:
            return Response(f'Failed to delete vote: {e}', status=400)

    @action(
        detail=True,
        methods=['post', 'put', 'patch'],
        permission_classes=[UpvotePaper]
    )
    def upvote(self, request, pk=None):
        paper = self.get_object()
        hub_ids = paper.hubs.values_list('id', flat=True)
        user = request.user

        vote_exists = find_vote(user, paper, Vote.UPVOTE)

        if vote_exists:
            return Response(
                'This vote already exists',
                status=status.HTTP_400_BAD_REQUEST
            )
        response = update_or_create_vote(user, paper, Vote.UPVOTE)

        invalidate_trending_cache(hub_ids)
        invalidate_top_rated_cache(hub_ids)
        invalidate_newest_cache(hub_ids)
        invalidate_most_discussed_cache(hub_ids)
        cache_key_paper = get_cache_key(request, 'paper')
        cache.delete(cache_key_paper)
        return response

    @action(
        detail=True,
        methods=['post', 'put', 'patch'],
        permission_classes=[DownvotePaper]
    )
    def downvote(self, request, pk=None):
        paper = self.get_object()
        hub_ids = paper.hubs.values_list('id', flat=True)
        user = request.user

        vote_exists = find_vote(user, paper, Vote.DOWNVOTE)

        if vote_exists:
            return Response(
                'This vote already exists',
                status=status.HTTP_400_BAD_REQUEST
            )
        response = update_or_create_vote(user, paper, Vote.DOWNVOTE)

        invalidate_trending_cache(hub_ids)
        invalidate_top_rated_cache(hub_ids)
        invalidate_newest_cache(hub_ids)
        invalidate_most_discussed_cache(hub_ids)
        cache_key_paper = get_cache_key(request, 'paper')
        cache.delete(cache_key_paper)
        return response

    @action(detail=False, methods=[POST])
    def check_url(self, request):
        url = request.data.get('url', None)
        url_is_pdf = check_url_contains_pdf(url)
        data = {'found_file': url_is_pdf}
        return Response(data, status=status.HTTP_200_OK)

    @staticmethod
    def search_by_csl_item(csl_item):
        """
        Perform an elasticsearch query for papers matching
        the input CSL_Item.
        """
        from elasticsearch_dsl import Search, Q
        search = Search(index="paper")
        title = csl_item.get('title', '')
        query = Q("match", title=title) | Q("match", paper_title=title)
        if csl_item.get('DOI'):
            query |= Q("match", doi=csl_item['DOI'])
        search.query(query)
        return search

    @action(detail=False, methods=['post'])
    def search_by_url(self, request):
        """
        Retrieve bibliographic metadata and potential paper matches
        from the database for `url` (specified via request post data).
        """
        url = request.data.get('url')
        data = {'url': url}

        if not url:
            return Response(
                "search_by_url requests must specify 'url'",
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            URLValidator()(url)
        except (ValidationError, Exception) as e:
            print(e)
            return Response(
                f"Double check that URL is valid: {url}",
                status=status.HTTP_400_BAD_REQUEST
            )

        url_is_pdf = check_url_contains_pdf(url)
        data['url_is_pdf'] = url_is_pdf

        try:
            csl_item = get_csl_item(url)
        except Exception as error:
            data['warning'] = f"Generating csl_item failed with:\n{error}"
            csl_item = None

        if csl_item:
            url_is_unsupported_pdf = url_is_pdf and csl_item.get('URL') == url
            data['url_is_unsupported_pdf'] = url_is_unsupported_pdf
            csl_item.url_is_unsupported_pdf = url_is_unsupported_pdf
            data['csl_item'] = csl_item
            data['oa_pdf_location'] = get_pdf_location_for_csl_item(csl_item)
            doi = csl_item.get('DOI', None)
            data['doi_already_in_db'] = (
                (doi is not None)
                and (len(Paper.objects.filter(doi=doi)) > 0)
            )

        if csl_item and request.data.get('search', False):
            # search existing papers
            search = self.search_by_csl_item(csl_item)
            try:
                search = search.execute()
            except ConnectionError:
                return Response(
                    "Search failed due to an elasticsearch ConnectionError.",
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            data['search'] = [hit.to_dict() for hit in search.hits]

        return Response(data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def get_hub_papers(self, request):
        # TODO: move this off the view and recalculate every 10 minutes
        # through the worker
        page_number = int(request.GET['page'])
        start_date = datetime.datetime.fromtimestamp(
            int(request.GET.get('start_date__gte', 0)),
            datetime.timezone.utc
        )
        end_date = datetime.datetime.fromtimestamp(
            int(request.GET.get('end_date__lte', 0)),
            datetime.timezone.utc
        )
        ordering = self._set_hub_paper_ordering(request)
        hub_id = request.GET.get('hub_id', 0)

        time_difference = end_date - start_date
        cache_pk = ''
        if time_difference.days == 365:
            cache_pk = f'{hub_id}_{ordering}_year'
        elif time_difference.days == 30 or time_difference.days == 31:
            cache_pk = f'{hub_id}_{ordering}_month'
        elif time_difference.days == 7:
            cache_pk = f'{hub_id}_{ordering}_week'
        else:
            cache_pk = f'{hub_id}_{ordering}_today'

        cache_key_hub = get_cache_key(None, 'hub', pk=cache_pk)
        cache_key_papers = get_cache_key(None, 'hub', pk='papers')
        cache_hit_hub = cache.get(cache_key_hub)
        cache_hit_papers = cache.get(cache_key_papers)
        cache_hit_exists = cache_hit_papers and cache_hit_hub

        if cache_hit_exists and page_number == 1:
            # cache.get_or_set()
            for item in cache_hit_hub:
                paper_id = item['id']
                try:
                    paper = Paper.objects.get(pk=paper_id)
                    item['user_vote'] = self.serializer_class(
                        context={'request': request}
                    ).get_user_vote(paper)
                except Exception as e:
                    log_error(e)
            page = self.paginate_queryset(cache_hit_papers)
            return self.get_paginated_response(
                {'data': cache_hit_hub, 'no_results': False}
            )

        papers = self._get_filtered_papers(hub_id)

        if 'hot_score' in ordering:
            # constant > (hours in month) ** gravity * (discussion_weight + 2)
            #INT_DIVISION = 90000000
            # num votes a comment is worth
            #DISCUSSION_WEIGHT = 2

            #gravity = 2.5
            #now_epoch = int(timezone.now().timestamp())
            #time_since_calc = (now_epoch - F('vote_avg_epoch')) / 3600 + 1

            #numerator = F('score') + F('discussion_count') * DISCUSSION_WEIGHT
            #inverse_divisor = (
            #    INT_DIVISION / ((time_since_calc) ** gravity)
            #)
            order_papers = papers.order_by(ordering)

        elif 'score' in ordering:
            upvotes = Count(
                'vote',
                filter=Q(
                    vote__vote_type=Vote.UPVOTE,
                    vote__updated_date__range=[start_date,end_date]
                )
            )
            downvotes = Count(
                'vote',
                filter=Q(
                    vote__vote_type=Vote.DOWNVOTE,
                    vote__updated_date__range=[start_date,end_date]
                )
            )

            order_papers = papers.annotate(
                score_in_time=upvotes - downvotes,
                score_all_time=F('score')
            ).order_by(ordering + '_in_time', ordering + '_all_time')

        elif 'discussed' in ordering:
            threads_count = Count(
                'threads',
                filter=Q(
                    threads__created_date__range=[start_date,end_date]
                )
            )
            comments_count = Count(
                'threads__comments',
                filter=Q(
                    threads__comments__created_date__range=[start_date,end_date]
                )
            )

            order_papers = papers.annotate(
                discussed=threads_count + comments_count,
                discussed_secondary=F('discussion_count')
            ).order_by(ordering, ordering + '_secondary')

        else:
            order_papers = papers.order_by(ordering)

        page = self.paginate_queryset(order_papers)
        context = self.get_serializer_context()
        serializer = HubPaperSerializer(page, many=True, context=context)
        serializer_data = serializer.data
        if page_number == 1:
            cache.set(cache_key_hub, serializer_data, timeout=60*60*24*7)
            cache.set(cache_key_papers, order_papers[:15], timeout=60*60*24*7)
        return self.get_paginated_response(
            {'data': serializer_data, 'no_results': False}
        )

    def _set_hub_paper_ordering(self, request):
        ordering = request.query_params.get('ordering', None)
        # TODO send correct ordering from frontend
        if ordering == 'top_rated':
            ordering = '-score'
        elif ordering == 'most_discussed':
            ordering = '-discussed'
        elif ordering == 'newest':
            ordering = '-uploaded_date'
        elif ordering == 'hot':
            ordering = '-hot_score'
        else:
            ordering = '-score'
        return ordering

    def _get_filtered_papers(self, hub_id):
        # hub_id = 0 is the homepage
        # we aren't on a specific hub so don't filter by that hub_id
        if int(hub_id) == 0:
            return self.get_queryset(prefetch=False).prefetch_related(*self.prefetch_lookups())
        return self.get_queryset(prefetch=False).filter(hubs=hub_id).prefetch_related(*self.prefetch_lookups())


class FigureViewSet(viewsets.ModelViewSet):
    queryset = Figure.objects.all()
    serializer_class = FigureSerializer

    permission_classes = [
        IsModeratorOrVerifiedAuthor
    ]

    def get_queryset(self):
        return self.queryset

    def get_figures(self, paper_id, figure_type=None):
        # Returns all figures
        paper = Paper.objects.get(id=paper_id)
        figures = self.get_queryset().filter(paper=paper)

        if figure_type:
            figures = figures.filter(figure_type=figure_type)

        figures = figures.order_by('-figure_type', 'created_date')
        figure_serializer = self.serializer_class(figures, many=True)
        return figure_serializer.data

    @action(
        detail=True,
        methods=['post'],
        permission_classes=[IsAuthor]
    )
    def add_figure(self, request, pk=None):
        paper = Paper.objects.get(id=pk)
        figure = request.files.get('figure')
        figure_type = request.data.get('figure_type')
        Figure.objects.create(
            paper=paper,
            file=figure,
            figure_type=figure_type
        )
        return Response(status=200)

    @action(
        detail=True,
        methods=['delete'],
        permission_classes=[IsAuthor]
    )
    def delete_figure(self, request, pk=None):
        figure = self.get_queryset().get(id=pk)
        figure.delete()
        return Response(status=200)

    @action(
        detail=True,
        methods=['get'],
        permission_classes=[IsAuthenticatedOrReadOnly]
    )
    def get_all_figures(self, request, pk=None):
        cache_key = get_cache_key(request, 'figure')
        cache_hit = cache.get(cache_key)
        if cache_hit is not None:
            return Response(
                {'data': cache_hit},
                status=status.HTTP_200_OK
            )

        serializer_data = self.get_figures(pk)
        cache.set(cache_key, serializer_data, timeout=60*60*24*7)
        return Response(
            {'data': serializer_data},
            status=status.HTTP_200_OK
        )

    @action(
        detail=True,
        methods=['get'],
        permission_classes=[IsAuthenticatedOrReadOnly]
    )
    def get_preview_figures(self, request, pk=None):
        # Returns pdf preview figures
        serializer_data = self.get_figures(pk, figure_type=Figure.PREVIEW)
        return Response(
            {'data': serializer_data},
            status=status.HTTP_200_OK
        )

    @action(
        detail=True,
        methods=['get'],
        permission_classes=[IsAuthenticatedOrReadOnly]
    )
    def get_regular_figures(self, request, pk=None):
        # Returns regular figures
        serializer_data = self.get_figures(pk, figure_type=Figure.FIGURE)
        return Response(
            {'data': serializer_data},
            status=status.HTTP_200_OK
        )


def find_vote(user, paper, vote_type):
    vote = Vote.objects.filter(
        paper=paper,
        created_by=user,
        vote_type=vote_type
    )
    if vote:
        return True
    return False


def update_or_create_vote(user, paper, vote_type):
    vote = retrieve_vote(user, paper)

    if vote:
        vote.vote_type = vote_type
        vote.save()
        return get_vote_response(vote, 200)
    vote = create_vote(user, paper, vote_type)
    return get_vote_response(vote, 201)


def get_vote_response(vote, status_code):
    """Returns Response with serialized `vote` data and `status_code`."""
    serializer = PaperVoteSerializer(vote)
    return Response(serializer.data, status=status_code)


def retrieve_vote(user, paper):
    try:
        return Vote.objects.get(
            paper=paper,
            created_by=user.id
        )
    except Vote.DoesNotExist:
        return None


def create_vote(user, paper, vote_type):
    vote = Vote.objects.create(
        created_by=user,
        paper=paper,
        vote_type=vote_type
    )
    return vote
