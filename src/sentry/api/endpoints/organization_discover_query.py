from __future__ import absolute_import

import re
import six
from functools32 import partial
from copy import deepcopy

from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied

from sentry.utils.dates import (
    parse_stats_period,
)

from sentry.api.serializers.rest_framework import ListField
from sentry.api.bases.organization import OrganizationPermission
from sentry.api.bases import OrganizationEndpoint
from sentry.api.paginator import GenericOffsetPaginator
from sentry.models import Project, ProjectStatus, OrganizationMember, OrganizationMemberTeam
from sentry.utils import snuba
from sentry import roles
from sentry import features


class OrganizationDiscoverQueryPermission(OrganizationPermission):
    scope_map = {
        'POST': ['org:read', 'project:read'],
    }


class DiscoverQuerySerializer(serializers.Serializer):
    projects = ListField(
        child=serializers.IntegerField(),
        required=True,
        allow_null=False,
    )
    start = serializers.DateTimeField(required=False)
    end = serializers.DateTimeField(required=False)
    range = serializers.CharField(required=False)
    fields = ListField(
        child=serializers.CharField(),
        required=False,
        allow_null=True,
    )
    limit = serializers.IntegerField(min_value=0, max_value=1000, required=False)
    rollup = serializers.IntegerField(required=False)
    orderby = serializers.CharField(required=False)
    conditions = ListField(
        child=ListField(),
        required=False,
        allow_null=True,
    )
    aggregations = ListField(
        child=ListField(),
        required=False,
        allow_null=True,
        default=[]
    )
    groupby = ListField(
        child=serializers.CharField(),
        required=False,
        allow_null=True,
    )
    turbo = serializers.BooleanField(required=False)

    def __init__(self, *args, **kwargs):
        super(DiscoverQuerySerializer, self).__init__(*args, **kwargs)
        self.member = OrganizationMember.objects.get(
            user=self.context['user'], organization=self.context['organization'])

        data = kwargs['data']

        fields = data.get('fields') or []

        match = next(
            (
                self.get_array_field(field).group(1)
                for field
                in fields
                if self.get_array_field(field) is not None
            ),
            None
        )
        self.arrayjoin = match if match else None

    def validate(self, data):
        data['arrayjoin'] = self.arrayjoin

        return data

    def validate_range(self, attrs, source):
        has_start = bool(attrs.get('start'))
        has_end = bool(attrs.get('end'))
        has_range = bool(attrs.get('range'))

        if has_start != has_end or has_range == has_start:
            raise serializers.ValidationError('Either start and end dates or range is required')

        # Populate start and end if only range is provided
        if (attrs.get(source)):
            delta = parse_stats_period(attrs[source])

            if (delta is None):
                raise serializers.ValidationError('Invalid range')

            attrs['start'] = timezone.now() - delta
            attrs['end'] = timezone.now()

        return attrs

    def validate_orderby(self, attrs, source):
        source = attrs.get(source)

        if source:
            order_by = source.replace('-', '') if source.startswith('-') else source
            fields = attrs.get('fields')
            aggregations = set(agg[2] for agg in attrs.get('aggregations')) if attrs.get('aggregations') else False

            if aggregations and fields:
                aggregations.add('time')
                if not any(order_by == agg for agg in aggregations) and not any(order_by == field for field in fields):
                    raise serializers.ValidationError('Invalid OrderBy - Must be in Fields or Aggregations')
            elif aggregations:
                aggregations.add('time')
                if not any(order_by == agg for agg in aggregations):
                    raise serializers.ValidationError('Invalid OrderBy - Must be in Fields or Aggregations')
            elif fields:
                print('fields')
                if not any(order_by == field for field in fields):
                    raise serializers.ValidationError('Invalid OrderBy - Must be in Fields or Aggregations')

        return attrs


    def validate_projects(self, attrs, source):
        organization = self.context['organization']
        member = self.member
        projects = attrs[source]

        org_projects = set(project[0] for project in self.context['projects'])

        if not set(projects).issubset(org_projects) or not self.has_projects_access(
                member, organization, projects):
            raise PermissionDenied

        return attrs

    def validate_conditions(self, attrs, source):
        # Handle error (exception_stacks), stack(exception_frames)
        if attrs.get(source):
            conditions = [self.get_condition(condition) for condition in attrs[source]]
            attrs[source] = conditions
        return attrs

    def validate_aggregations(self, attrs, source):
        valid_functions = set(['count()', 'uniq', 'avg'])
        requested_functions = set(agg[0] for agg in attrs[source])

        if not requested_functions.issubset(valid_functions):
            invalid_functions = ', '.join((requested_functions - valid_functions))

            raise serializers.ValidationError(
                u'Invalid aggregate function - {}'.format(invalid_functions)
            )

        return attrs

    def get_array_field(self, field):
        pattern = r"^(error|stack)\..+"
        return re.search(pattern, field)

    def get_condition(self, condition):
        array_field = self.get_array_field(condition[0])
        has_equality_operator = condition[1] in ('=', '!=')

        # Cast boolean values to 1 / 0
        if isinstance(condition[2], bool):
            condition[2] = int(condition[2])

        # Apply has function to any array field if it's = / != and not part of arrayjoin
        if array_field and has_equality_operator and (array_field.group(1) != self.arrayjoin):
            value = condition[2]

            if (isinstance(value, six.string_types)):
                value = u"'{}'".format(value)

            bool_value = 1 if condition[1] == '=' else 0

            return [['has', [array_field.group(0), value]], '=', bool_value]

        return condition

    def has_projects_access(self, member, organization, requested_projects):
        has_global_access = roles.get(member.role).is_global
        if has_global_access:
            return True

        member_project_list = Project.objects.filter(
            organization=organization,
            teams__in=OrganizationMemberTeam.objects.filter(
                organizationmember=member,
            ).values('team'),
        ).values_list('id', flat=True)

        return set(requested_projects).issubset(set(member_project_list))


class OrganizationDiscoverQueryEndpoint(OrganizationEndpoint):
    permission_classes = (OrganizationDiscoverQueryPermission, )

    def get_json_type(self, snuba_type):
        """
        Convert Snuba/Clickhouse type to JSON type
        Default is string
        """

        # Ignore Nullable part
        nullable_match = re.search(r'^Nullable\((.+)\)$', snuba_type)

        if nullable_match:
            snuba_type = nullable_match.group(1)
        # Check for array

        array_match = re.search(r'^Array\(.+\)$', snuba_type)
        if array_match:
            return 'array'

        types = {
            'UInt8': 'boolean',
            'UInt16': 'integer',
            'UInt32': 'integer',
            'UInt64': 'integer',
            'Float32': 'number',
            'Float64': 'number',
        }

        return types.get(snuba_type, 'string')

    def handle_results(self, snuba_results, requested_query, projects):
        if 'project_name' in requested_query['selected_columns']:
            project_name_index = requested_query['selected_columns'].index('project_name')
            snuba_results['meta'].insert(
                project_name_index, {
                    'name': 'project_name', 'type': 'String'})
            if 'project_id' not in requested_query['selected_columns']:
                snuba_results['meta'] = [
                    field for field in snuba_results['meta'] if field['name'] != 'project_id'
                ]

            for result in snuba_results['data']:
                result['project_name'] = projects[result['project_id']]
                if 'project_id' not in requested_query['selected_columns']:
                    del result['project_id']

        if 'project_name' in requested_query['groupby']:
            project_name_index = requested_query['groupby'].index('project_name')
            snuba_results['meta'].insert(
                project_name_index, {
                    'name': 'project_name', 'type': 'String'})
            if 'project_id' not in requested_query['groupby']:
                snuba_results['meta'] = [
                    field for field in snuba_results['meta'] if field['name'] != 'project_id'
                ]

            for result in snuba_results['data']:
                result['project_name'] = projects[result['project_id']]
                if 'project_id' not in requested_query['groupby']:
                    del result['project_id']

        # Convert snuba types to json types
        for col in snuba_results['meta']:
            col['type'] = self.get_json_type(col.get('type'))

        return snuba_results

    def do_query(self, projects, request, **kwargs):
        requested_query = deepcopy(kwargs)

        selected_columns = kwargs['selected_columns']
        groupby_columns = kwargs['groupby']

        if 'project_name' in requested_query['selected_columns']:
            selected_columns.remove('project_name')
            if 'project_id' not in selected_columns:
                selected_columns.append('project_id')

        if 'project_name' in requested_query['groupby']:
            groupby_columns.remove('project_name')
            if 'project_id' not in groupby_columns:
                groupby_columns.append('project_id')

        for aggregation in kwargs['aggregations']:
            if aggregation[1] == 'project_name':
                aggregation[1] = 'project_id'

        if not kwargs['aggregations']:

            data_fn = partial(
                snuba.transform_aliases_and_query,
                referrer='discover',
                **kwargs
            )
            return self.paginate(
                request=request,
                on_results=lambda results: self.handle_results(results, requested_query, projects),
                paginator=GenericOffsetPaginator(data_fn=data_fn),
                max_per_page=1000
            )
        else:
            snuba_results = snuba.transform_aliases_and_query(
                referrer='discover',
                **kwargs
            )
            return Response(self.handle_results(
                snuba_results,
                requested_query,
                projects,
            ), status=200)

    def post(self, request, organization):

        if not features.has('organizations:discover', organization, actor=request.user):
            return self.respond(status=404)

        projects = Project.objects.filter(
            organization=organization,
            status=ProjectStatus.VISIBLE,
        ).values_list('id', 'slug')

        serializer = DiscoverQuerySerializer(
            data=request.DATA, context={
                'organization': organization, 'projects': projects, 'user': request.user})

        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        serialized = serializer.object

        has_aggregations = len(serialized.get('aggregations')) > 0

        selected_columns = [] if has_aggregations else serialized.get('fields')

        projects_map = {}
        for project in projects:
            projects_map[project[0]] = project[1]

        # Make sure that all selected fields are in the group by clause if there
        # are aggregations
        groupby = serialized.get('groupby') or []
        fields = serialized.get('fields') or []
        if has_aggregations:
            for field in fields:
                if field not in groupby:
                    groupby.append(field)

        return self.do_query(
            projects=projects_map,
            start=serialized.get('start'),
            end=serialized.get('end'),
            groupby=groupby,
            selected_columns=selected_columns,
            conditions=serialized.get('conditions'),
            orderby=serialized.get('orderby'),
            limit=serialized.get('limit'),
            aggregations=serialized.get('aggregations'),
            rollup=serialized.get('rollup'),
            filter_keys={'project_id': serialized.get('projects')},
            arrayjoin=serialized.get('arrayjoin'),
            request=request,
            turbo=serialized.get('turbo'),
        )
