"""
This is built from the package ecs_deploy.ecs
https://github.com/fabfuel/ecs-deploy/blob/develop/ecs_deploy/ecs.py

The package defaults to copying old environment config, applying
value changes onto it and merging new configs. This means, environment
variables cannot be deleted.

To update the package:
Copy the new contents, but ensure that apply_container_environment is retained
or updated. If the package supports deleting configs, use that.
"""

from datetime import datetime
from json import dumps

from boto3.session import Session
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from dateutil.tz.tz import tzlocal


class EcsClient(object):
    def __init__(self, access_key_id=None, secret_access_key=None,
                 region=None, profile=None):
        session = Session(aws_access_key_id=access_key_id,
                          aws_secret_access_key=secret_access_key,
                          region_name=region,
                          profile_name=profile)
        self.boto = session.client(u'ecs')

    def describe_services(self, cluster_name, service_name):
        return self.boto.describe_services(
            cluster=cluster_name,
            services=[service_name]
        )

    def describe_task_definition(self, task_definition_arn):
        try:
            return self.boto.describe_task_definition(
                taskDefinition=task_definition_arn
            )
        except ClientError:
            raise UnknownTaskDefinitionError(
                u'Unknown task definition arn: %s' % task_definition_arn
            )

    def list_tasks(self, cluster_name, service_name):
        return self.boto.list_tasks(
            cluster=cluster_name,
            serviceName=service_name
        )

    def list_task_definitions(self, family_prefix, status='ACTIVE', sort='DESC'):
        response = self.boto.list_task_definitions(familyPrefix=family_prefix, status=status, sort=sort)
        return response['taskDefinitionArns']

    def describe_tasks(self, cluster_name, task_arns):
        return self.boto.describe_tasks(cluster=cluster_name, tasks=task_arns)

    def register_task_definition(self, family, containers, volumes, role_arn, cpu=False, memory=False, execution_role_arn=None,
                                 requires_compatibilities=[], network_mode='bridge'):
        fargate_td = {}
        if 'FARGATE' in requires_compatibilities:
            fargate_td = {
                'requiresCompatibilities': requires_compatibilities or [],
                'cpu': cpu,
                'memory': memory,
            }
        return self.boto.register_task_definition(
            family=family,
            containerDefinitions=containers,
            volumes=volumes,
            executionRoleArn=execution_role_arn,
            taskRoleArn=role_arn or u'',
            networkMode=network_mode,
            **fargate_td
        )

    def deregister_task_definition(self, task_definition_arn):
        return self.boto.deregister_task_definition(
            taskDefinition=task_definition_arn
        )

    def update_service(self, cluster, service, desired_count, task_definition):
        return self.boto.update_service(
            cluster=cluster,
            service=service,
            desiredCount=desired_count,
            taskDefinition=task_definition
        )

    def run_task(self, cluster, task_definition, count, started_by, overrides):
        return self.boto.run_task(
            cluster=cluster,
            taskDefinition=task_definition,
            count=count,
            startedBy=started_by,
            overrides=overrides
        )


class EcsService(dict):
    def __init__(self, cluster, service_definition=None, **kwargs):
        self._cluster = cluster
        super(EcsService, self).__init__(service_definition, **kwargs)

    def set_desired_count(self, desired_count):
        self[u'desiredCount'] = desired_count

    def set_task_definition(self, task_definition):
        self[u'taskDefinition'] = task_definition.arn

    @property
    def cluster(self):
        return self._cluster

    @property
    def name(self):
        return self.get(u'serviceName')

    @property
    def task_definition(self):
        return self.get(u'taskDefinition')

    @property
    def desired_count(self):
        return self.get(u'desiredCount')

    @property
    def deployment_created_at(self):
        for deployment in self.get(u'deployments'):
            if deployment.get(u'status') == u'PRIMARY':
                return deployment.get(u'createdAt')
        return datetime.now()

    @property
    def deployment_updated_at(self):
        for deployment in self.get(u'deployments'):
            if deployment.get(u'status') == u'PRIMARY':
                return deployment.get(u'updatedAt')
        return datetime.now()

    @property
    def errors(self):
        return self.get_warnings(
            since=self.deployment_updated_at
        )

    @property
    def older_errors(self):
        return self.get_warnings(
            since=self.deployment_created_at,
            until=self.deployment_updated_at
        )

    def get_warnings(self, since=None, until=None):
        since = since or self.deployment_created_at
        until = until or datetime.now(tz=tzlocal())
        errors = {}
        for event in self.get(u'events'):
            if u'unable' not in event[u'message']:
                continue
            if since < event[u'createdAt'] < until:
                errors[event[u'createdAt']] = event[u'message']
        return errors


class EcsTaskDefinition(dict):
    def __init__(self, task_definition=None, **kwargs):
        super(EcsTaskDefinition, self).__init__(task_definition, **kwargs)
        self._diff = []

    @property
    def containers(self):
        return self.get(u'containerDefinitions')

    @property
    def container_names(self):
        for container in self.get(u'containerDefinitions'):
            yield container[u'name']

    @property
    def volumes(self):
        return self.get(u'volumes')

    @property
    def arn(self):
        return self.get(u'taskDefinitionArn')

    @property
    def requires_compatibilities(self):
        return self.get(u'requiresCompatibilities')

    @property
    def execution_role_arn(self):
        return self.get(u'executionRoleArn')

    @property
    def network_mode(self):
        return self.get(u'networkMode')

    @property
    def cpu(self):
        return self.get(u'cpu')

    @property
    def memory(self):
        return self.get(u'memory')

    @property
    def family(self):
        return self.get(u'family')

    @property
    def role_arn(self):
        return self.get(u'taskRoleArn')

    @property
    def revision(self):
        return self.get(u'revision')

    @property
    def family_revision(self):
        return '%s:%d' % (self.get(u'family'), self.get(u'revision'))

    @property
    def diff(self):
        return self._diff

    def get_overrides(self):
        override = dict()
        overrides = []
        for diff in self.diff:
            if override.get('name') != diff.container:
                override = dict(name=diff.container)
                overrides.append(override)
            if diff.field == 'command':
                override['command'] = self.get_overrides_command(diff.value)
            elif diff.field == 'environment':
                override['environment'] = self.get_overrides_env(diff.value)
        return overrides

    @staticmethod
    def get_overrides_command(command):
        return command.split(' ')

    @staticmethod
    def get_overrides_env(env):
        return [{"name": e, "value": env[e]} for e in env]

    def set_images(self, tag=None, **images):
        self.validate_container_options(**images)
        for container in self.containers:
            if container.get('name').endswith('-sidecar'):
                continue
            if container[u'name'] in images:
                new_image = images[container[u'name']]
                diff = EcsTaskDefinitionDiff(
                    container=container[u'name'],
                    field=u'image',
                    value=new_image,
                    old_value=container[u'image']
                )
                self._diff.append(diff)
                container[u'image'] = new_image
            elif tag:
                image_definition = container[u'image'].rsplit(u':', 1)
                new_image = u'%s:%s' % (image_definition[0], tag.strip())
                diff = EcsTaskDefinitionDiff(
                    container=container[u'name'],
                    field=u'image',
                    value=new_image,
                    old_value=container[u'image']
                )
                self._diff.append(diff)
                container[u'image'] = new_image

    def set_commands(self, **commands):
        self.validate_container_options(**commands)
        for container in self.containers:
            if container[u'name'] in commands:
                new_command = commands[container[u'name']]
                diff = EcsTaskDefinitionDiff(
                    container=container[u'name'],
                    field=u'command',
                    value=new_command,
                    old_value=container.get(u'command')
                )
                self._diff.append(diff)
                container[u'command'] = [new_command]

    def set_environment(self, environment_list):
        environment = {}

        for env in environment_list:
            environment.setdefault(env[0], {})
            environment[env[0]][env[1]] = env[2]

        self.validate_container_options(**environment)
        for container in self.containers:
            if container[u'name'] in environment:
                self.apply_container_environment(
                    container=container,
                    new_environment=environment[container[u'name']]
                )

    def apply_container_environment(self, container, new_environment):
        old_environment = {
            env['name']: env['valueFrom'] for env in container.get(
                'secrets',
                {}
            )
        }
        merged_environment = {var[0]: var[1] for var in new_environment}

        diff = EcsTaskDefinitionDiff(
            container[u'name'],
            u'secrets',
            merged_environment,
            old_environment
        )
        self._diff.append(diff)
        container[u'secrets'] = [
            {
                "name": e,
                "valueFrom": merged_environment[e]
            } for e in merged_environment
        ]
        if not container.get('name', '').endswith('-sidecar'):
            if container[u'environment'] is not None:
                container[u'environment'] = []

    def validate_container_options(self, **container_options):
        for container_name in container_options:
            if container_name not in self.container_names:
                raise UnknownContainerError(
                    u'Unknown container: %s' % container_name
                )

    def set_role_arn(self, role_arn):
        if role_arn:
            diff = EcsTaskDefinitionDiff(
                container=None,
                field=u'role_arn',
                value=role_arn,
                old_value=self[u'taskRoleArn']
            )
            self[u'taskRoleArn'] = role_arn
            self._diff.append(diff)


class EcsTaskDefinitionDiff(object):
    def __init__(self, container, field, value, old_value):
        self.container = container
        self.field = field
        self.value = value
        self.old_value = old_value

    def __repr__(self):
        if self.container:
            return u"Changed %s of container '%s' to: %s (was: %s)" % (
                self.field,
                self.container,
                dumps(self.value),
                dumps(self.old_value)
            )
        else:
            return u"Changed %s to: %s (was: %s)" % (
                self.field,
                dumps(self.value),
                dumps(self.old_value)
            )


class EcsAction(object):
    def __init__(self, client, cluster_name, service_name):
        self._client = client
        self._cluster_name = cluster_name
        self._service_name = service_name

        try:
            if service_name:
                self._service = self.get_service()
        except IndexError:
            raise EcsConnectionError(
                u'An error occurred when calling the DescribeServices '
                u'operation: Service not found.'
            )
        except ClientError as e:
            raise EcsConnectionError(str(e))
        except NoCredentialsError:
            raise EcsConnectionError(
                u'Unable to locate credentials. Configure credentials '
                u'by running "aws configure".'
            )

    def get_service(self):
        services_definition = self._client.describe_services(
            cluster_name=self._cluster_name,
            service_name=self._service_name
        )
        return EcsService(
            cluster=self._cluster_name,
            service_definition=services_definition[u'services'][0]
        )

    def get_current_task_definition(self, service):
        task_definition_payload = self._client.describe_task_definition(
            task_definition_arn=service.task_definition
        )
        task_definition = EcsTaskDefinition(
            task_definition=task_definition_payload[u'taskDefinition']
        )
        return task_definition

    def get_task_definition(self, task_definition):
        task_definition_payload = self._client.describe_task_definition(
            task_definition_arn=task_definition
        )
        task_definition = EcsTaskDefinition(
            task_definition=task_definition_payload[u'taskDefinition']
        )
        return task_definition

    def update_task_definition(self, task_definition):
        fargate_td = {}
        if task_definition.requires_compatibilities and 'FARGATE' in task_definition.requires_compatibilities:
            fargate_td = {
                'requires_compatibilities': task_definition.requires_compatibilities or [],
                'cpu' : task_definition.cpu or u'',
                'memory' : task_definition.memory or u'',

            }
        response = self._client.register_task_definition(
            family=task_definition.family,
            containers=task_definition.containers,
            volumes=task_definition.volumes,
            role_arn=task_definition.role_arn,
            execution_role_arn=task_definition.execution_role_arn if task_definition.execution_role_arn else boto3.resource('iam').Role('ecsTaskExecutionRole').arn,
            network_mode=task_definition.network_mode or u'bridge',
            **fargate_td
        )
        new_task_definition = EcsTaskDefinition(response[u'taskDefinition'])
        self._client.deregister_task_definition(task_definition.arn)
        return new_task_definition

    def update_service(self, service):
        response = self._client.update_service(
            cluster=service.cluster,
            service=service.name,
            desired_count=service.desired_count,
            task_definition=service.task_definition
        )
        return EcsService(self._cluster_name, response[u'service'])

    def is_deployed(self, service):
        if len(service[u'deployments']) != 1:
            return False
        running_tasks = self._client.list_tasks(
            cluster_name=service.cluster,
            service_name=service.name
        )
        if not running_tasks[u'taskArns']:
            return service.desired_count == 0
        running_count = self.get_running_tasks_count(
            service=service,
            task_arns=running_tasks[u'taskArns']
        )
        return service.desired_count == running_count

    def get_running_tasks_count(self, service, task_arns):
        running_count = 0
        tasks_details = self._client.describe_tasks(
            cluster_name=self._cluster_name,
            task_arns=task_arns
        )
        for task in tasks_details[u'tasks']:
            arn = task[u'taskDefinitionArn']
            status = task[u'lastStatus']
            if arn == service.task_definition and status == u'RUNNING':
                running_count += 1
        return running_count

    @property
    def client(self):
        return self._client

    @property
    def service(self):
        return self._service

    @property
    def cluster_name(self):
        return self._cluster_name

    @property
    def service_name(self):
        return self._service_name


class DeployAction(EcsAction):
    def deploy(self, task_definition):
        self._service.set_task_definition(task_definition)
        return self.update_service(self._service)


class ScaleAction(EcsAction):
    def scale(self, desired_count):
        self._service.set_desired_count(desired_count)
        return self.update_service(self._service)


class RunAction(EcsAction):
    def __init__(self, client, cluster_name):
        super(RunAction, self).__init__(client, cluster_name, None)
        self._client = client
        self._cluster_name = cluster_name
        self.started_tasks = []

    def run(self, task_definition, count, started_by):
        result = self._client.run_task(
            cluster=self._cluster_name,
            task_definition=task_definition.family_revision,
            count=count,
            started_by=started_by,
            overrides=dict(containerOverrides=task_definition.get_overrides())
        )
        self.started_tasks = result['tasks']
        return True


class EcsError(Exception):
    pass


class EcsConnectionError(EcsError):
    pass


class UnknownContainerError(EcsError):
    pass


class TaskPlacementError(EcsError):
    pass


class UnknownTaskDefinitionError(EcsError):
    pass
