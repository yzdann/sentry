from __future__ import absolute_import

from abc import ABCMeta, abstractmethod

import logging
import time
import six
import datetime
from datetime import timedelta
from hashlib import md5

from django.utils import timezone

from sentry import options
from sentry.api.event_search import convert_search_filter_to_snuba_query
from sentry.api.paginator import DateTimePaginator, SequencePaginator, Paginator
from sentry.constants import ALLOWED_FUTURE_DELTA
from sentry.models import Group, Release, Project
from sentry.utils import snuba, metrics
from sentry.snuba.dataset import Dataset


def get_search_filter(search_filters, name, operator):
    """
    Finds the value of a search filter with the passed name and operator. If
    multiple values are found, returns the most restrictive value
    :param search_filters: collection of `SearchFilter` objects
    :param name: Name of the field to find
    :param operator: '<' or '>'
    :return: The value of the field if found, else None
    """
    assert operator in ("<", ">")
    comparator = max if operator.startswith(">") else min
    found_val = None
    for search_filter in search_filters:
        # Note that we check operator with `startswith` here so that we handle
        # <, <=, >, >=
        if search_filter.key.name == name and search_filter.operator.startswith(operator):
            val = search_filter.value.raw_value
            found_val = comparator(val, found_val) if found_val else val
    return found_val


@six.add_metaclass(ABCMeta)
class AbstractQueryExecutor:
    """This class serves as a template for Query Executors.
    We subclass it in order to implement query methods (we use it to implement two classes: joined Postgres+Snuba queries, and Snuba only queries)
    It's used to keep the query logic out of the actual search backend,
    which can now just build query parameters and use the appropriate query executor to run the query
    """

    EMPTY_RESULT = Paginator(Group.objects.none()).get_result()
    TABLE_ALIAS = ""

    @abstractmethod
    def query(self, *args, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def calculate_hits(self, *args, **kwargs):
        raise NotImplementedError

    def _get_dataset(self):
        if not self.QUERY_DATASET:
            raise NotImplementedError
        else:
            return self.QUERY_DATASET

    def snuba_search(
        self,
        start,
        end,
        project_ids,
        environment_ids,
        sort_field,
        cursor=None,
        group_ids=None,
        limit=None,
        offset=0,
        get_sample=False,
        search_filters=None,
    ):
        """
        Returns a tuple of:
        * a sorted list of (group_id, group_score) tuples sorted descending by score,
        * the count of total results (rows) available for this query.
        """

        filters = {"project_id": project_ids}

        if environment_ids is not None:
            filters["environment"] = environment_ids

        if group_ids:
            filters[self.TABLE_ALIAS + "issue"] = sorted(group_ids)

        conditions = []
        having = []
        for search_filter in search_filters:
            if (
                # Don't filter on issue fields here, they're not available
                search_filter.key.name in self.issue_only_fields
                or
                # We special case date
                search_filter.key.name == "date"
            ):
                continue
            converted_filter = convert_search_filter_to_snuba_query(search_filter)
            converted_filter, converted_name = self._transform_converted_filter(
                search_filter, converted_filter, project_ids, environment_ids
            )
            # field_name = self.TABLE_ALIAS + search_filter.key.name
            # Ensure that no user-generated tags that clashes with aggregation_defs is added to having
            if converted_name in self.aggregation_defs and not search_filter.key.is_tag:
                having.append(converted_filter)
            else:
                conditions.append(converted_filter)

        extra_aggregations = self.dependency_aggregations.get(sort_field, [])
        required_aggregations = set([sort_field, "total"] + extra_aggregations)
        for h in having:
            alias = h[0]
            required_aggregations.add(alias)

        aggregations = []
        for alias in required_aggregations:
            aggregations.append(self.aggregation_defs[alias] + [alias])

        if cursor is not None:
            having.append((sort_field, ">=" if cursor.is_prev else "<=", cursor.value))

        selected_columns = []
        if get_sample:
            query_hash = md5(repr(conditions)).hexdigest()[:8]
            selected_columns.append(
                ("cityHash64", ("'{}'".format(query_hash), self.TABLE_ALIAS + "issue"), "sample")
            )
            sort_field = "sample"
            orderby = [sort_field]
            referrer = "search_sample"
        else:
            # Get the top matching groups by score, i.e. the actual search results
            # in the order that we want them.
            orderby = [
                "-{}".format(sort_field),
                self.TABLE_ALIAS + "issue",
            ]  # ensure stable sort within the same score
            referrer = "search"

        snuba_results = snuba.dataset_query(
            dataset=self._get_dataset(),
            start=start,
            end=end,
            selected_columns=selected_columns,
            groupby=[self.TABLE_ALIAS + "issue"],
            conditions=conditions,
            having=having,
            filter_keys=filters,
            aggregations=aggregations,
            orderby=orderby,
            referrer=referrer,
            limit=limit,
            offset=offset,
            totals=True,  # Needs to have totals_mode=after_having_exclusive so we get groups matching HAVING only
            turbo=get_sample,  # Turn off FINAL when in sampling mode
            sample=1,  # Don't use clickhouse sampling, even when in turbo mode.
        )
        rows = snuba_results["data"]
        total = snuba_results["totals"]["total"]

        if not get_sample:
            metrics.timing("snuba.search.num_result_groups", len(rows))

        return [(row[self.TABLE_ALIAS + "issue"], row[sort_field]) for row in rows], total

    def _transform_converted_filter(
        self, search_filter, converted_filter, project_ids, environment_ids=None
    ):
        """This method serves as a hook - after we convert the search_filter into a snuba compatible filter (which converts it in a general dataset ambigious method),
            we may want to transform the query - maybe change the value (time formats, translate value into id (like turning Release `version` into `id`) or vice versa),  alias fields, etc.
            By default, no transformation is done.
        """
        return converted_filter, search_filter.key.name


def snuba_search(
    start,
    end,
    project_ids,
    environment_ids,
    sort_field,
    cursor=None,
    candidate_ids=None,
    limit=None,
    offset=0,
    get_sample=False,
    search_filters=None,
):
    """
    This function doesn't strictly benefit from or require being pulled out of the main
    query method above, but the query method is already large and this function at least
    extracts most of the Snuba-specific logic.

    Returns a tuple of:
     * a sorted list of (group_id, group_score) tuples sorted descending by score,
     * the count of total results (rows) available for this query.
    """
    filters = {"project_id": project_ids}

    if environment_ids is not None:
        filters["environment"] = environment_ids

    if candidate_ids:
        filters["issue"] = sorted(candidate_ids)

    conditions = []
    having = []
    for search_filter in search_filters:
        if (
            # Don't filter on issue fields here, they're not available
            search_filter.key.name in issue_only_fields
            or
            # We special case date
            search_filter.key.name == "date"
        ):
            continue
        converted_filter = convert_search_filter_to_snuba_query(search_filter)

        # Ensure that no user-generated tags that clashes with aggregation_defs is added to having
        if search_filter.key.name in aggregation_defs and not search_filter.key.is_tag:
            having.append(converted_filter)
        else:
            conditions.append(converted_filter)

    extra_aggregations = dependency_aggregations.get(sort_field, [])
    required_aggregations = set([sort_field, "total"] + extra_aggregations)
    for h in having:
        alias = h[0]
        required_aggregations.add(alias)

    aggregations = []
    for alias in required_aggregations:
        aggregations.append(aggregation_defs[alias] + [alias])

    if cursor is not None:
        having.append((sort_field, ">=" if cursor.is_prev else "<=", cursor.value))

    selected_columns = []
    if get_sample:
        query_hash = md5(repr(conditions)).hexdigest()[:8]
        selected_columns.append(("cityHash64", ("'{}'".format(query_hash), "issue"), "sample"))
        sort_field = "sample"
        orderby = [sort_field]
        referrer = "search_sample"
    else:
        # Get the top matching groups by score, i.e. the actual search results
        # in the order that we want them.
        orderby = ["-{}".format(sort_field), "issue"]  # ensure stable sort within the same score
        referrer = "search"

    snuba_results = snuba.dataset_query(
        dataset=Dataset.Events,
        start=start,
        end=end,
        selected_columns=selected_columns,
        groupby=["issue"],
        conditions=conditions,
        having=having,
        filter_keys=filters,
        aggregations=aggregations,
        orderby=orderby,
        referrer=referrer,
        limit=limit,
        offset=offset,
        totals=True,  # Needs to have totals_mode=after_having_exclusive so we get groups matching HAVING only
        turbo=get_sample,  # Turn off FINAL when in sampling mode
        sample=1,  # Don't use clickhouse sampling, even when in turbo mode.
    )
    rows = snuba_results["data"]
    total = snuba_results["totals"]["total"]

    if not get_sample:
        metrics.timing("snuba.search.num_result_groups", len(rows))

    return [(row["issue"], row[sort_field]) for row in rows], total


class PostgresSnubaQueryExecutor(AbstractQueryExecutor):
    QUERY_DATASET = snuba.Dataset.Events
    ISSUE_FIELD_NAME = "issue"

    logger = logging.getLogger("sentry.search.postgressnuba")
    dependency_aggregations = {"priority": ["last_seen", "times_seen"]}
    issue_only_fields = set(
        [
            "query",
            "status",
            "bookmarked_by",
            "assigned_to",
            "unassigned",
            "subscribed_by",
            "active_at",
            "first_release",
            "first_seen",
        ]
    )
    sort_strategies = {
        "date": "last_seen",
        "freq": "times_seen",
        "new": "first_seen",
        "priority": "priority",
    }

    aggregation_defs = {
        "times_seen": ["count()", ""],
        "first_seen": ["multiply(toUInt64(min(timestamp)), 1000)", ""],
        "last_seen": ["multiply(toUInt64(max(timestamp)), 1000)", ""],
        # https://github.com/getsentry/sentry/blob/804c85100d0003cfdda91701911f21ed5f66f67c/src/sentry/event_manager.py#L241-L271
        "priority": ["toUInt64(plus(multiply(log(times_seen), 600), last_seen))", ""],
        # Only makes sense with WITH TOTALS, returns 1 for an individual group.
        "total": ["uniq", ISSUE_FIELD_NAME],
    }

    def query(
        self,
        projects,
        retention_window_start,
        group_queryset,
        environments,
        sort_by,
        limit,
        cursor,
        count_hits,
        paginator_options,
        search_filters,
        date_from,
        date_to,
        *args,
        **kwargs
    ):

        now = timezone.now()
        end = None
        end_params = filter(None, [date_to, get_search_filter(search_filters, "date", "<")])
        if end_params:
            end = min(end_params)

        if not end:
            end = now + ALLOWED_FUTURE_DELTA

            # This search is for some time window that ends with "now",
            # so if the requested sort is `date` (`last_seen`) and there
            # are no other Snuba-based search predicates, we can simply
            # return the results from Postgres.
            if (
                cursor is None
                and sort_by == "date"
                and not environments
                and
                # This handles tags and date parameters for search filters.
                not [
                    sf
                    for sf in search_filters
                    if sf.key.name not in self.issue_only_fields.union(["date"])
                ]
            ):
                group_queryset = group_queryset.order_by("-last_seen")
                paginator = DateTimePaginator(group_queryset, "-last_seen", **paginator_options)
                # When its a simple django-only search, we count_hits like normal
                return paginator.get_result(limit, cursor, count_hits=count_hits)

        # TODO: Presumably we only want to search back to the project's max
        # retention date, which may be closer than 90 days in the past, but
        # apparently `retention_window_start` can be None(?), so we need a
        # fallback.
        retention_date = max(filter(None, [retention_window_start, now - timedelta(days=90)]))

        # TODO: We should try and consolidate all this logic together a little
        # better, maybe outside the backend. Should be easier once we're on
        # just the new search filters
        start_params = [date_from, retention_date, get_search_filter(search_filters, "date", ">")]
        start = max(filter(None, start_params))

        end = max([retention_date, end])

        if start == retention_date and end == retention_date:
            # Both `start` and `end` must have been trimmed to `retention_date`,
            # so this entire search was against a time range that is outside of
            # retention. We'll return empty results to maintain backwards compatibility
            # with Django search (for now).
            return self.EMPTY_RESULT

        if start >= end:
            # TODO: This maintains backwards compatibility with Django search, but
            # in the future we should find a way to notify the user that their search
            # is invalid.
            return self.EMPTY_RESULT

        # Here we check if all the django filters reduce the set of groups down
        # to something that we can send down to Snuba in a `group_id IN (...)`
        # clause.
        max_candidates = options.get("snuba.search.max-pre-snuba-candidates")
        too_many_candidates = False
        group_ids = list(group_queryset.values_list("id", flat=True)[: max_candidates + 1])
        metrics.timing("snuba.search.num_candidates", len(group_ids))
        if not group_ids:
            # no matches could possibly be found from this point on
            metrics.incr("snuba.search.no_candidates", skip_internal=False)
            return self.EMPTY_RESULT
        elif len(group_ids) > max_candidates:
            # If the pre-filter query didn't include anything to significantly
            # filter down the number of results (from 'first_release', 'query',
            # 'status', 'bookmarked_by', 'assigned_to', 'unassigned',
            # 'subscribed_by', 'active_at_from', or 'active_at_to') then it
            # might have surpassed the `max_candidates`. In this case,
            # we *don't* want to pass candidates down to Snuba, and instead we
            # want Snuba to do all the filtering/sorting it can and *then* apply
            # this queryset to the results from Snuba, which we call
            # post-filtering.
            metrics.incr("snuba.search.too_many_candidates", skip_internal=False)
            too_many_candidates = True
            group_ids = []

        sort_field = self.sort_strategies[sort_by]
        chunk_growth = options.get("snuba.search.chunk-growth-rate")
        max_chunk_size = options.get("snuba.search.max-chunk-size")
        chunk_limit = limit
        offset = 0
        num_chunks = 0
        hits = None

        paginator_results = self.EMPTY_RESULT
        result_groups = []
        result_group_ids = set()

        max_time = options.get("snuba.search.max-total-chunk-time-seconds")
        time_start = time.time()

        # Do smaller searches in chunks until we have enough results
        # to answer the query (or hit the end of possible results). We do
        # this because a common case for search is to return 100 groups
        # sorted by `last_seen`, and we want to avoid returning all of
        # a project's groups and then post-sorting them all in Postgres
        # when typically the first N results will do.
        while (time.time() - time_start) < max_time:
            num_chunks += 1

            # grow the chunk size on each iteration to account for huge projects
            # and weird queries, up to a max size
            chunk_limit = min(int(chunk_limit * chunk_growth), max_chunk_size)
            # but if we have group_ids always query for at least that many items
            chunk_limit = max(chunk_limit, len(group_ids))

            # {group_id: group_score, ...}
            snuba_groups, total = self.snuba_search(
                start=start,
                end=end,
                project_ids=[p.id for p in projects],
                environment_ids=environments and [environment.id for environment in environments],
                sort_field=sort_field,
                cursor=cursor,
                group_ids=group_ids,
                limit=chunk_limit,
                offset=offset,
                search_filters=search_filters,
            )
            metrics.timing("snuba.search.num_snuba_results", len(snuba_groups))
            count = len(snuba_groups)
            more_results = count >= limit and (offset + limit) < total
            offset += len(snuba_groups)

            if not snuba_groups:
                break

            if group_ids:
                # pre-filtered candidates were passed down to Snuba, so we're
                # finished with filtering and these are the only results. Note
                # that because we set the chunk size to at least the size of
                # the group_ids, we know we got all of them (ie there are
                # no more chunks after the first)
                result_groups = snuba_groups
                # if count_hits and hits is None:
                # hits = len(snuba_groups)
            else:
                # pre-filtered candidates were *not* passed down to Snuba,
                # so we need to do post-filtering to verify Sentry DB predicates
                filtered_group_ids = group_queryset.filter(
                    id__in=[gid for gid, _ in snuba_groups]
                ).values_list("id", flat=True)

                group_to_score = dict(snuba_groups)
                for group_id in filtered_group_ids:
                    if group_id in result_group_ids:
                        # because we're doing multiple Snuba queries, which
                        # happen outside of a transaction, there is a small possibility
                        # of groups moving around in the sort scoring underneath us,
                        # so we at least want to protect against duplicates
                        continue

                    group_score = group_to_score[group_id]
                    result_group_ids.add(group_id)
                    result_groups.append((group_id, group_score))

            if hits is None:
                hits = self.calculate_hits(
                    group_ids,
                    snuba_groups,
                    too_many_candidates,
                    sort_field,
                    projects,
                    retention_window_start,
                    group_queryset,
                    environments,
                    sort_by,
                    limit,
                    cursor,
                    count_hits,
                    paginator_options,
                    search_filters,
                    start,
                    end,
                )

            # TODO do we actually have to rebuild this SequencePaginator every time
            # or can we just make it after we've broken out of the loop?
            paginator_results = SequencePaginator(
                [(score, id) for (id, score) in result_groups], reverse=True, **paginator_options
            ).get_result(limit, cursor, known_hits=hits)

            # break the query loop for one of three reasons:
            # * we started with Postgres candidates and so only do one Snuba query max
            # * the paginator is returning enough results to satisfy the query (>= the limit)
            # * there are no more groups in Snuba to post-filter
            if group_ids or len(paginator_results.results) >= limit or not more_results:
                break

        # HACK: We're using the SequencePaginator to mask the complexities of going
        # back and forth between two databases. This causes a problem with pagination
        # because we're 'lying' to the SequencePaginator (it thinks it has the entire
        # result set in memory when it does not). For this reason we need to make some
        # best guesses as to whether the `prev` and `next` cursors have more results.

        if len(paginator_results.results) == limit and more_results:
            # Because we are going back and forth between DBs there is a small
            # chance that we will hand the SequencePaginator exactly `limit`
            # items. In this case the paginator will assume there are no more
            # results, so we need to override the `next` cursor's results.
            paginator_results.next.has_results = True

        if cursor is not None and (not cursor.is_prev or len(paginator_results.results) > 0):
            # If the user passed a cursor, and it isn't already a 0 result `is_prev`
            # cursor, then it's worth allowing them to go back a page to check for
            # more results.
            paginator_results.prev.has_results = True

        metrics.timing("snuba.search.num_chunks", num_chunks)

        groups = Group.objects.in_bulk(paginator_results.results)
        paginator_results.results = [groups[k] for k in paginator_results.results if k in groups]

        return paginator_results

    def calculate_hits(
        self,
        group_ids,
        snuba_groups,
        too_many_candidates,
        sort_field,
        projects,
        retention_window_start,
        group_queryset,
        environments,
        sort_by,
        limit,
        cursor,
        count_hits,
        paginator_options,
        search_filters,
        start,
        end,
    ):
        if count_hits is False:
            return None
        elif too_many_candidates or cursor is not None:
            # If we had too many candidates to reasonably pass down to snuba,
            # or if we have a cursor that bisects the overall result set (such
            # that our query only sees results on one side of the cursor) then
            # we need an alternative way to figure out the total hits that this
            # query has.

            # To do this, we get a sample of groups matching the snuba side of
            # the query, and see how many of those pass the post-filter in
            # postgres. This should give us an estimate of the total number of
            # snuba matches that will be overall matches, which we can use to
            # get an estimate for X-Hits.

            # The sampling is not simple random sampling. It will return *all*
            # matching groups if there are less than N groups matching the
            # query, or it will return a random, deterministic subset of N of
            # the groups if there are more than N overall matches. This means
            # that the "estimate" is actually an accurate result when there are
            # less than N matching groups.

            # The number of samples required to achieve a certain error bound
            # with a certain confidence interval can be calculated from a
            # rearrangement of the normal approximation (Wald) confidence
            # interval formula:
            #
            # https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval
            #
            # Effectively if we want the estimate to be within +/- 10% of the
            # real value with 95% confidence, we would need (1.96^2 * p*(1-p))
            # / 0.1^2 samples. With a starting assumption of p=0.5 (this
            # requires the most samples) we would need 96 samples to achieve
            # +/-10% @ 95% confidence.

            sample_size = options.get("snuba.search.hits-sample-size")
            snuba_groups, snuba_total = self.snuba_search(
                start=start,
                end=end,
                project_ids=[p.id for p in projects],
                environment_ids=environments and [environment.id for environment in environments],
                sort_field=sort_field,
                limit=sample_size,
                offset=0,
                get_sample=True,
                search_filters=search_filters,
            )
            snuba_count = len(snuba_groups)
            if snuba_count == 0:
                return (
                    0
                )  # Maybe check for 0 hits and return EMPTY_RESULT in ::query? self.EMPTY_RESULT
            else:
                filtered_count = group_queryset.filter(
                    id__in=[gid for gid, _ in snuba_groups]
                ).count()

                hit_ratio = filtered_count / float(snuba_count)
                hits = int(hit_ratio * snuba_total)
        elif group_ids:
            return len(snuba_groups)

        return hits


class SnubaOnlyQueryExecutor(AbstractQueryExecutor):
    QUERY_DATASET = snuba.Dataset.Groups
    TABLE_ALIAS = "events."
    ISSUE_FIELD_NAME = TABLE_ALIAS + "issue"
    logger = logging.getLogger("sentry.search.snubagroups")

    # TODO: Define these using table alias somehow?
    # Since I think that is the only difference, other than issue_only_fields having less items

    dependency_aggregations = {"priority": ["events.last_seen", "times_seen"]}
    issue_only_fields = set(
        ["query", "bookmarked_by", "assigned_to", "unassigned", "subscribed_by"]
    )
    sort_strategies = {
        # TODO: If not using environment filters, could these sort methods use last_seen and first_seen from groups instead? so only add prefix conditionally?
        "date": "events.last_seen",
        "freq": "times_seen",
        "new": "events.first_seen",
        "priority": "priority",
    }

    aggregation_defs = {
        "times_seen": ["count()", ""],
        "events.first_seen": ["multiply(toUInt64(min(events.timestamp)), 1000)", ""],
        "events.last_seen": ["multiply(toUInt64(max(events.timestamp)), 1000)", ""],
        "priority": ["toUInt64(plus(multiply(log(times_seen), 600), `events.last_seen`))", ""],
        "total": ["uniq", ISSUE_FIELD_NAME],
    }

    def _transform_converted_filter(
        self, search_filter, converted_filter, project_ids=None, environment_ids=None
    ):
        converted_name = search_filter.key.name

        special_date_names = ["active_at", "first_seen", "last_seen"]
        if search_filter.key.name in special_date_names:
            # Need to get '2018-02-06T03:35:54' format out of 1517888878000 format
            datetime_value = datetime.datetime.fromtimestamp(converted_filter[2] / 1000)
            datetime_value = datetime_value.replace(microsecond=0).isoformat().replace("+00:00", "")
            converted_filter[2] = datetime_value

        # TODO: There is a better way to do this...the issue is that the table/alias is forked on environments, which can't be used in constrain_column_to_dataset
        if search_filter.key.name in ["first_seen", "last_seen"]:  # , "first_release"]:
            if environment_ids is not None:
                table_alias = "events."
                # return None, None #??? remove as a filter, since we are going to pre/post filter?
            else:
                table_alias = "groups."

            if isinstance(converted_filter[0], list):
                converted_filter[0][1][0] = table_alias + converted_filter[0][1][0]
                converted_name = converted_filter[0][1][0]
            else:
                converted_filter[0] = table_alias + converted_filter[0]
                converted_name = converted_filter[0]

        if search_filter.key.name == "first_release":
            # The filter's value will be the release's "version". Snuba only knows about ID. So we convert version to id here.
            release = Release.objects.filter(
                version=converted_filter[2],
                organization_id=Project.objects.get(id=project_ids[0]).organization_id,
            )
            if not release:
                # TODO: This means there will be no results and we do not need to run this query!
                # right now it could lead to undesired results...if -1 is a real release id
                converted_filter[
                    2
                ] = -1  # this is a number im hoping will never be a real release id
            else:
                converted_filter[2] = release[0].id

        # if search_filter.key.name.startswith("tags["):
        #     if isinstance(converted_filter[0], list):
        #         converted_filter[0][1][0] = "events." + converted_filter[0][1][0]
        #         converted_name = converted_filter[0][1][0]
        #     else:
        #         converted_filter[0] = "events." + converted_filter[0]
        #         converted_name = converted_filter[0]

        return converted_filter, converted_name

    def query(
        self,
        projects,
        retention_window_start,
        group_queryset,
        environments,
        sort_by,
        limit,
        cursor,
        count_hits,
        paginator_options,
        search_filters,
        date_from,
        date_to,
        *args,
        **kwargs
    ):

        now = timezone.now()
        end = None
        end_params = filter(None, [date_to, get_search_filter(search_filters, "date", "<")])
        if end_params:
            end = min(end_params)

        if not end:
            end = now + ALLOWED_FUTURE_DELTA

        retention_date = max(filter(None, [retention_window_start, now - timedelta(days=90)]))

        start_params = [date_from, retention_date, get_search_filter(search_filters, "date", ">")]
        start = max(filter(None, start_params))

        end = max([retention_date, end])

        if start == retention_date and end == retention_date:
            return self.EMPTY_RESULT

        if start >= end:
            return self.EMPTY_RESULT

        sort_field = self.sort_strategies[sort_by]
        chunk_growth = options.get("snuba.search.chunk-growth-rate")
        max_chunk_size = options.get("snuba.search.max-chunk-size")
        chunk_limit = limit
        offset = 0
        num_chunks = 0
        hits = None

        paginator_results = self.EMPTY_RESULT
        result_groups = []
        result_group_ids = set()

        max_time = options.get("snuba.search.max-total-chunk-time-seconds")
        time_start = time.time()

        while (time.time() - time_start) < max_time:
            num_chunks += 1

            chunk_limit = min(int(chunk_limit * chunk_growth), max_chunk_size)

            snuba_groups, total = self.snuba_search(
                start=start,
                end=end,
                project_ids=[p.id for p in projects],
                environment_ids=environments and [environment.id for environment in environments],
                sort_field=sort_field,
                cursor=cursor,
                limit=chunk_limit,
                offset=offset,
                search_filters=search_filters,
            )
            metrics.timing("snuba.search.num_snuba_results", len(snuba_groups))
            count = len(snuba_groups)
            more_results = count >= limit and (offset + limit) < total
            offset += len(snuba_groups)

            if not snuba_groups:
                break

            group_to_score = dict(snuba_groups)
            for group_id in group_to_score:
                if group_id in result_group_ids:
                    continue
                group_score = group_to_score[group_id]
                result_group_ids.add(group_id)
                result_groups.append((group_id, group_score))

            if hits is None:
                hits = self.calculate_hits(
                    snuba_groups,
                    sort_field,
                    projects,
                    retention_window_start,
                    group_queryset,
                    environments,
                    sort_by,
                    limit,
                    cursor,
                    count_hits,
                    paginator_options,
                    search_filters,
                    start,
                    end,
                )

            paginator_results = SequencePaginator(
                [(score, id) for (id, score) in result_groups], reverse=True, **paginator_options
            ).get_result(limit, cursor, known_hits=hits)

            if len(paginator_results.results) >= limit or not more_results:
                break

        if len(paginator_results.results) == limit and more_results:
            paginator_results.next.has_results = True

        if cursor is not None and (not cursor.is_prev or len(paginator_results.results) > 0):
            paginator_results.prev.has_results = True

        metrics.timing("snuba.search.num_chunks", num_chunks)

        groups = Group.objects.in_bulk(paginator_results.results)
        paginator_results.results = [groups[k] for k in paginator_results.results if k in groups]

        return paginator_results

    def calculate_hits(
        self,
        snuba_groups,
        sort_field,
        projects,
        retention_window_start,
        group_queryset,
        environments,
        sort_by,
        limit,
        cursor,
        count_hits,
        paginator_options,
        search_filters,
        start,
        end,
    ):
        # TODO: This needs a proper aggregate added to it to just count the results - I don't this this is correct.
        _, hits = self.snuba_search(
            start=start,
            end=end,
            project_ids=[p.id for p in projects],
            environment_ids=environments and [environment.id for environment in environments],
            sort_field=sort_field,
            limit=None,
            offset=0,
            get_sample=False,
            search_filters=search_filters,
        )
        return hits
