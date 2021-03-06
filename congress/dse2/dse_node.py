# Copyright (c) 2016 VMware, Inc. All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#

import json
import six
import uuid

import eventlet
eventlet.monkey_patch()  # for using oslo.messaging w/ eventlet executor

from futurist import periodics
from oslo_concurrency import lockutils
from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_log import log as logging
import oslo_messaging as messaging
from oslo_messaging import exceptions as messaging_exceptions
from oslo_utils import importutils
from oslo_utils import strutils
from oslo_utils import uuidutils

from congress.datasources import constants
from congress.db import api as db
from congress.db import datasources as datasources_db
from congress.db import db_ds_table_data
from congress.dse2.control_bus import DseNodeControlBus
from congress import exception


LOG = logging.getLogger(__name__)


_dse_opts = [
    cfg.StrOpt('bus_id', default='bus',
               help='Unique ID of this DSE bus'),
    cfg.IntOpt('dse_ping_timeout', default=5,
               help='RPC short timeout in seconds; used to ping destination'),
    cfg.IntOpt('dse_long_timeout', default=120,
               help='RPC long timeout in seconds; used on potentially long '
               'running requests such as datasource action and PE row query'),
]
cfg.CONF.register_opts(_dse_opts)


class DseNode(object):
    """Addressable entity participating on the DSE message bus.

    The Data Services Engine (DSE) is comprised of one or more DseNode
    instances that each may run one or more DataService instances.  All
    communication between data services uses the DseNode interface.

    Attributes:
        node_id: The unique ID of this node on the DSE.
        messaging_config: Configuration options for the message bus.  See
                          oslo.messaging for more details.
        node_rpc_endpoints: List of object instances exposing a remotely
                            invokable interface.
    """
    RPC_VERSION = '1.0'
    EXCHANGE = 'congress'
    CONTROL_TOPIC = 'congress-control'
    SERVICE_TOPIC_PREFIX = 'congress-service-'

    def node_rpc_target(self, namespace=None, server=None, fanout=False):
        return messaging.Target(exchange=self.EXCHANGE,
                                topic=self._add_partition(self.CONTROL_TOPIC),
                                version=self.RPC_VERSION,
                                namespace=namespace,
                                server=server,
                                fanout=fanout)

    def service_rpc_target(self, service_id, namespace=None, server=None,
                           fanout=False):
        topic = self._add_partition(self.SERVICE_TOPIC_PREFIX + service_id)
        return messaging.Target(exchange=self.EXCHANGE,
                                topic=topic,
                                version=self.RPC_VERSION,
                                namespace=namespace,
                                server=server,
                                fanout=fanout)

    def _add_partition(self, topic, partition_id=None):
        """Create a seed-specific version of an oslo-messaging topic."""
        partition_id = partition_id or self.partition_id
        if partition_id is None:
            return topic
        return topic + "-" + str(partition_id)

    def __init__(self, messaging_config, node_id, node_rpc_endpoints,
                 partition_id=None):
        # Note(ekcs): temporary setting to disable use of diffs and sequencing
        #   to avoid muddying the process of a first dse2 system test.
        # TODO(ekcs,dse2): remove when differential update is standard
        self.always_snapshot = False

        self.messaging_config = messaging_config
        self.node_id = node_id
        self.node_rpc_endpoints = node_rpc_endpoints
        # unique identifier shared by all nodes that can communicate
        self.partition_id = partition_id or cfg.CONF.bus_id or "bus"
        self.node_rpc_endpoints.append(DseNodeEndpoints(self))
        self._running = False
        self._services = []
        self.instance = uuid.uuid4()  # uuid to help recognize node_id clash
        # TODO(dse2): add detection and logging/rectifying for node_id clash?
        self.context = self._message_context()
        self.transport = messaging.get_transport(
            self.messaging_config,
            allowed_remote_exmods=[exception.__name__, ])
        self._rpctarget = self.node_rpc_target(self.node_id, self.node_id)
        self._rpc_server = messaging.get_rpc_server(
            self.transport, self._rpctarget, self.node_rpc_endpoints,
            executor='eventlet')

        # # keep track of what publisher/tables local services subscribe to
        # subscribers indexed by publisher and table:
        # {publisher_id ->
        #     {table_name -> set_of_subscriber_ids}}
        self.subscriptions = {}

        # Note(ekcs): A little strange that _control_bus starts before self?
        self._control_bus = DseNodeControlBus(self)
        self.register_service(self._control_bus)
        # load configured drivers
        self.loaded_drivers = self.load_drivers()
        self.periodic_tasks = None
        self.sync_thread = None
        self.start()

    def __del__(self):
        self.stop()
        self.wait()

    def __repr__(self):
        return self.__class__.__name__ + "<%s>" % self.node_id

    def _message_context(self):
        return {'node_id': self.node_id, 'instance': str(self.instance)}

    # Note(thread-safety): blocking function
    @lockutils.synchronized('register_service')
    def register_service(self, service):
        assert service.node is None
        if self.service_object(service.service_id):
            msg = ('Service %s already exsists on the node %s'
                   % (service.service_id, self.node_id))
            raise exception.DataServiceError(msg)

        service.always_snapshot = self.always_snapshot
        service.node = self
        self._services.append(service)
        service._target = self.service_rpc_target(service.service_id,
                                                  server=self.node_id)
        service._rpc_server = messaging.get_rpc_server(
            self.transport, service._target, service.rpc_endpoints(),
            executor='eventlet')

        service.start()

        LOG.debug('<%s> Service %s RPC Server listening on %s',
                  self.node_id, service.service_id, service._target)

    # Note(thread-safety): blocking function
    def unregister_service(self, service_id=None, uuid_=None):
        """Unregister service from DseNode matching on service_id or uuid_

        Only one should be supplied. No-op if no matching service found.
        """
        service = self.service_object(service_id=service_id, uuid_=uuid_)
        if service is not None:
            self._services.remove(service)
            service.stop()
            # Note(thread-safety): blocking call
            service.wait()
        LOG.debug("Service %s stopped on node %s", service.service_id,
                  self.node_id)

    def get_services(self, hidden=False):
        """Return all local service objects."""
        if hidden:
            return self._services
        return [s for s in self._services if s.service_id[0] != '_']

    def get_global_service_names(self, hidden=False):
        """Return names of all services on all nodes."""
        services = self.get_services(hidden=hidden)
        local_services = [s.service_id for s in services]
        # Also, check services registered on other nodes
        peer_nodes = self.dse_status()['peers']
        peer_services = []
        for node in peer_nodes.values():
            peer_services.extend(
                [srv['service_id'] for srv in node['services']])
        return set(local_services + peer_services)

    def service_object(self, service_id=None, uuid_=None):
        """Return the service object requested.

        Search by service_id or uuid_ (only one should be supplied).
        None if not found.
        """
        if service_id is not None:
            if uuid_ is not None:
                raise TypeError('service_object() cannot accept both args '
                                'service_id and uuid_')
            for s in self._services:
                if s.service_id == service_id:
                    return s
        elif uuid_ is not None:
            for s in self._services:
                if getattr(s, 'ds_id', None) == uuid_:
                    return s
        else:
            raise TypeError('service_object() requires service_id or '
                            'uuid_ argument, but neither is given.')
        return None

    def start(self):
        LOG.debug("<%s> DSE Node '%s' starting with %s sevices...",
                  self.node_id, self.node_id, len(self._services))

        # Start Node RPC server
        self._rpc_server.start()
        LOG.debug('<%s> Node RPC Server listening on %s',
                  self.node_id, self._rpctarget)

        # Start Service RPC server(s)
        for s in self._services:
            s.start()
            LOG.debug('<%s> Service %s RPC Server listening on %s',
                      self.node_id, s.service_id, s._target)

        self._running = True

    def stop(self):
        if self._running is False:
            return

        LOG.info("Stopping DSE node '%s'", self.node_id)
        self.stop_datasource_synchronizer()
        for s in self._services:
            s.stop()
        self._rpc_server.stop()
        self._running = False

    # Note(thread-safety): blocking function
    def wait(self):
        for s in self._services:
            # Note(thread-safety): blocking call
            s.wait()
        # Note(thread-safety): blocking call
        self._rpc_server.wait()

    def dse_status(self):
        """Return latest observation of DSE status."""
        return self._control_bus.dse_status()

    def is_valid_service(self, service_id):
        return service_id in self.get_global_service_names(hidden=True)

    # Note(thread-safety): blocking function
    def invoke_node_rpc(self, node_id, method, kwargs=None, timeout=None):
        """Invoke RPC method on a DSE Node.

        Args:
            node_id: The ID of the node on which to invoke the call.
            method: The method name to call.
            kwargs: A dict of method arguments.

        Returns:
            The result of the method invocation.

        Raises: MessagingTimeout, RemoteError, MessageDeliveryFailure
        """
        if kwargs is None:
            kwargs = {}
        target = self.node_rpc_target(server=node_id)
        LOG.trace("<%s> Invoking RPC '%s' on %s", self.node_id, method, target)
        client = messaging.RPCClient(self.transport, target, timeout=timeout)
        return client.call(self.context, method, **kwargs)

    # Note(thread-safety): blocking function
    def broadcast_node_rpc(self, method, kwargs=None):
        """Invoke RPC method on all DSE Nodes.

        Args:
            method: The method name to call.
            kwargs: A dict of method arguments.

        Returns:
            None - Methods are invoked asynchronously and results are dropped.

        Raises: RemoteError, MessageDeliveryFailure
        """
        if kwargs is None:
            kwargs = {}
        target = self.node_rpc_target(fanout=True)
        LOG.trace("<%s> Casting RPC '%s' on %s", self.node_id, method, target)
        client = messaging.RPCClient(self.transport, target)
        client.cast(self.context, method, **kwargs)

    # Note(thread-safety): blocking function
    def invoke_service_rpc(
            self, service_id, method, kwargs=None, timeout=None, local=False,
            retry=None):
        """Invoke RPC method on a DSE Service.

        Args:
            service_id: The ID of the data service on which to invoke the call.
            method: The method name to call.
            kwargs: A dict of method arguments.

        Returns:
            The result of the method invocation.

        Raises: MessagingTimeout, RemoteError, MessageDeliveryFailure, NotFound
        """
        target = self.service_rpc_target(
            service_id, server=(self.node_id if local else None))
        LOG.trace("<%s> Preparing to invoking RPC '%s' on %s",
                  self.node_id, method, target)
        client = messaging.RPCClient(self.transport, target, timeout=timeout,
                                     retry=retry)
        if not self.is_valid_service(service_id):
            try:
                # First ping the destination to fail fast if unresponsive
                LOG.trace("<%s> Checking responsiveness before invoking RPC "
                          "'%s' on %s", self.node_id, method, target)
                client.prepare(timeout=cfg.CONF.dse_ping_timeout).call(
                    self.context, 'ping')
            except (messaging_exceptions.MessagingTimeout,
                    messaging_exceptions.MessageDeliveryFailure):
                msg = "service '%s' could not be found"
                raise exception.NotFound(msg % service_id)
        if kwargs is None:
            kwargs = {}
        try:
            LOG.trace(
                "<%s> Invoking RPC '%s' on %s", self.node_id, method, target)
            result = client.call(self.context, method, **kwargs)
        except (messaging_exceptions.MessagingTimeout,
                messaging_exceptions.MessageDeliveryFailure):
            msg = "Request to service '%s' timed out"
            raise exception.NotFound(msg % service_id)
        LOG.trace("<%s> RPC call returned: %s", self.node_id, result)
        return result

    # Note(thread-safety): blocking function
    def broadcast_service_rpc(self, service_id, method, kwargs=None):
        """Invoke RPC method on all instances of service_id.

        Args:
            service_id: The ID of the data service on which to invoke the call.
            method: The method name to call.
            kwargs: A dict of method arguments.

        Returns:
            None - Methods are invoked asynchronously and results are dropped.

        Raises: RemoteError, MessageDeliveryFailure
        """
        if kwargs is None:
            kwargs = {}
        if not self.is_valid_service(service_id):
            msg = "service '%s' is not a registered service"
            raise exception.NotFound(msg % service_id)

        target = self.service_rpc_target(service_id, fanout=True)
        LOG.trace("<%s> Casting RPC '%s' on %s", self.node_id, method, target)
        client = messaging.RPCClient(self.transport, target)
        client.cast(self.context, method, **kwargs)

    # Note(ekcs): non-sequenced publish retained to simplify rollout of dse2
    #   to be replaced by handle_publish_sequenced
    # Note(thread-safety): blocking function
    def publish_table(self, publisher, table, data):
        """Invoke RPC method on all insances of service_id.

        Args:
            service_id: The ID of the data service on which to invoke the call.
            method: The method name to call.
            kwargs: A dict of method arguments.

        Returns:
            None - Methods are invoked asynchronously and results are dropped.

        Raises: RemoteError, MessageDeliveryFailure
        """
        LOG.trace("<%s> Publishing from '%s' table %s: %s",
                  self.node_id, publisher, table, data)
        self.broadcast_node_rpc(
            "handle_publish",
            {'publisher': publisher, 'table': table, 'data': data})

    # Note(thread-safety): blocking function
    def publish_table_sequenced(
            self, publisher, table, data, is_snapshot, seqnum):
        """Invoke RPC method on all insances of service_id.

        Args:
            service_id: The ID of the data service on which to invoke the call.
            method: The method name to call.
            kwargs: A dict of method arguments.

        Returns:
            None - Methods are invoked asynchronously and results are dropped.

        Raises: RemoteError, MessageDeliveryFailure
        """
        LOG.trace("<%s> Publishing from '%s' table %s: %s",
                  self.node_id, publisher, table, data)
        self.broadcast_node_rpc(
            "handle_publish_sequenced",
            {'publisher': publisher, 'table': table,
             'data': data, 'is_snapshot': is_snapshot, 'seqnum': seqnum})

    def table_subscribers(self, publisher, table):
        """List services on this node that subscribes to publisher/table."""
        return self.subscriptions.get(
            publisher, {}).get(table, [])

    # Note(thread-safety): blocking function
    def subscribe_table(self, subscriber, publisher, table):
        """Prepare local service to receives publications from target/table."""
        # data structure: {service -> {target -> set-of-tables}
        LOG.trace("subscribing %s to %s:%s", subscriber, publisher, table)
        if publisher not in self.subscriptions:
            self.subscriptions[publisher] = {}
        if table not in self.subscriptions[publisher]:
            self.subscriptions[publisher][table] = set()
        self.subscriptions[publisher][table].add(subscriber)

        # oslo returns [] instead of set(), so handle that case directly
        if self.always_snapshot:
            # Note(thread-safety): blocking call
            snapshot = self.invoke_service_rpc(
                publisher, "get_snapshot", {'table': table})
            return self.to_set_of_tuples(snapshot)
        else:
            # Note(thread-safety): blocking call
            snapshot_seqnum = self.invoke_service_rpc(
                publisher, "get_last_published_data_with_seqnum",
                {'table': table})
            return snapshot_seqnum

    def get_subscription(self, service_id):
        """Return publisher/tables subscribed by service: service_id

        Return data structure:
        {publisher_id -> set of tables}
        """
        result = {}
        for publisher in self.subscriptions:
            for table in self.subscriptions[publisher]:
                if service_id in self.subscriptions[publisher][table]:
                    try:
                        result[publisher].add(table)
                    except KeyError:
                        result[publisher] = set([table])
        return result

    def to_set_of_tuples(self, snapshot):
        try:
            return set([tuple(x) for x in snapshot])
        except TypeError:
            return snapshot

    def unsubscribe_table(self, subscriber, publisher, table):
        """Remove subscription for local service to target/table."""
        if publisher not in self.subscriptions:
            return False
        if table not in self.subscriptions[publisher]:
            return False
        self.subscriptions[publisher][table].discard(subscriber)
        if len(self.subscriptions[publisher][table]) == 0:
            del self.subscriptions[publisher][table]
        if len(self.subscriptions[publisher]) == 0:
            del self.subscriptions[publisher]

    def _update_tables_with_subscriber(self):
        # not thread-safe: assumes each dseNode is single-threaded
        peers = self.dse_status()['peers']
        for s in self.get_services():
            sid = s.service_id
            # first, include subscriptions within the node, if any
            tables_with_subs = set(self.subscriptions.get(sid, {}))
            # then add subscriptions from other nodes
            for peer_id in peers:
                if sid in peers[peer_id]['subscribed_tables']:
                    tables_with_subs |= peers[
                        peer_id]['subscribed_tables'][sid]
            # call DataService hooks
            if hasattr(s, 'on_first_subs'):
                added = tables_with_subs - s._published_tables_with_subscriber
                if len(added) > 0:
                    s.on_first_subs(added)
            if hasattr(s, 'on_no_subs'):
                removed = \
                    s._published_tables_with_subscriber - tables_with_subs
                if len(removed) > 0:
                    s.on_no_subs(removed)
            s._published_tables_with_subscriber = tables_with_subs

    # Driver CRUD.  Maybe belongs in a subclass of DseNode?
    # Note(thread-safety): blocking function?
    def load_drivers(self):
        """Load all configured drivers and check no name conflict"""
        result = {}
        for driver_path in cfg.CONF.drivers:
            # Note(thread-safety): blocking call?
            obj = importutils.import_class(driver_path)
            driver = obj.get_datasource_info()
            if driver['id'] in result:
                raise exception.BadConfig(_("There is a driver loaded already"
                                          "with the driver name of %s")
                                          % driver['id'])
            driver['module'] = driver_path
            result[driver['id']] = driver
        return result

    def get_driver_info(self, driver):
        driver = self.loaded_drivers.get(driver)
        if not driver:
            raise exception.DriverNotFound(id=driver)
        return driver

    def get_drivers_info(self):
        return self.loaded_drivers

    def get_driver_schema(self, drivername):
        driver = self.get_driver_info(drivername)
        # Note(thread-safety): blocking call?
        obj = importutils.import_class(driver['module'])
        return obj.get_schema()

    # Datasource CRUD.  Maybe belongs in a subclass of DseNode?
    # Note(thread-safety): blocking function
    def get_datasource(cls, id_):
        """Return the created datasource."""
        # Note(thread-safety): blocking call
        result = datasources_db.get_datasource(id_)
        if not result:
            raise exception.DatasourceNotFound(id=id_)
        return cls.make_datasource_dict(result)

    # Note(thread-safety): blocking function
    def get_datasources(self, filter_secret=False):
        """Return the created datasources as recorded in the DB.

        This returns what datasources the database contains, not the
        datasources that this server instance is running.
        """
        results = []
        for datasource in datasources_db.get_datasources():
            result = self.make_datasource_dict(datasource)
            if filter_secret:
                # driver_info knows which fields should be secret
                driver_info = self.get_driver_info(result['driver'])
                try:
                    for hide_field in driver_info['secret']:
                        result['config'][hide_field] = "<hidden>"
                except KeyError:
                    pass
            results.append(result)
        return results

    def start_datasource_synchronizer(self):
        callables = [(self.synchronize, None, {})]
        self.periodic_tasks = periodics.PeriodicWorker(callables)
        if self._running:
            self.sync_thread = eventlet.spawn_n(self.periodic_tasks.start)

    def stop_datasource_synchronizer(self):
        if self.periodic_tasks:
            self.periodic_tasks.stop()
            self.periodic_tasks.wait()
            self.periodic_tasks = None
        if self.sync_thread:
            eventlet.greenthread.kill(self.sync_thread)
            self.sync_thread = None

    @periodics.periodic(spacing=(cfg.CONF.datasource_sync_period or 60))
    def synchronize(self):
        try:
            self.synchronize_datasources()
        except Exception:
            LOG.exception("synchronize_datasources failed")

    def synchronize_datasources(self):
        LOG.info("Synchronizing running datasources")
        added = 0
        removed = 0
        datasources = self.get_datasources(filter_secret=False)
        db_datasources = []
        # Look for datasources in the db, but not in the services.
        for configured_ds in datasources:
            db_datasources.append(configured_ds['id'])
            active_ds = self.service_object(uuid_=configured_ds['id'])
            # If datasource is not enabled, unregister the service
            if not configured_ds['enabled']:
                if active_ds:
                    LOG.debug("unregistering %s service, datasource disabled "
                              "in DB.", active_ds.service_id)
                    self.unregister_service(active_ds.service_id)
                    removed = removed + 1
                continue
            if active_ds is None:
                # service is not up, create the service
                LOG.debug("registering %s service on node %s",
                          configured_ds['name'], self.node_id)
                service = self.create_datasource_service(configured_ds)
                self.register_service(service)
                added = added + 1

        # Unregister the services which are not in DB
        active_ds_services = [s for s in self._services
                              if getattr(s, 'type', '') == 'datasource_driver']
        db_datasources_set = set(db_datasources)
        stale_services = [s for s in active_ds_services
                          if s.ds_id not in db_datasources_set]
        for s in stale_services:
            LOG.debug("unregistering %s service, datasource not found in DB ",
                      s.service_id)
            self.unregister_service(uuid_=s.ds_id)
            removed = removed + 1

        LOG.info("synchronize_datasources, added %d removed %d on node %s",
                 added, removed, self.node_id)

        # Will there be a case where datasource configs differ? update of
        # already created datasource is not supported anyway? so is below
        # code required?

        # if not self._config_eq(configured_ds, active_ds):
        #    LOG.debug('configured and active disagree: %s %s',
        #              strutils.mask_password(active_ds),
        #              strutils.mask_password(configured_ds))

        #    LOG.info('Reloading datasource: %s',
        #             strutils.mask_password(configured_ds))
        #    self.delete_datasource(configured_ds['name'],
        #                           update_db=False)
        #    self.add_datasource(configured_ds, update_db=False)

    # def _config_eq(self, db_config, active_config):
    #     return (db_config['name'] == active_config.service_id and
    #             db_config['config'] == active_config.service_info['args'])

    def delete_missing_driver_datasources(self):
        removed = 0
        for datasource in datasources_db.get_datasources():
            try:
                self.get_driver_info(datasource.driver)
            except exception.DriverNotFound:
                ds_dict = self.make_datasource_dict(datasource)
                self.delete_datasource(ds_dict)
                removed = removed+1
                LOG.debug("Deleted datasource with config %s ",
                          strutils.mask_password(ds_dict))

        LOG.info("Datsource cleanup completed, removed %d datasources",
                 removed)

    def make_datasource_dict(self, req, fields=None):
        result = {'id': req.get('id') or uuidutils.generate_uuid(),
                  'name': req.get('name'),
                  'driver': req.get('driver'),
                  'description': req.get('description'),
                  'type': None,
                  'enabled': req.get('enabled', True)}
        # NOTE(arosen): we store the config as a string in the db so
        # here we serialize it back when returning it.
        if isinstance(req.get('config'), six.string_types):
            result['config'] = json.loads(req['config'])
        else:
            result['config'] = req.get('config')

        return self._fields(result, fields)

    def _fields(self, resource, fields):
        if fields:
            return dict(((key, item) for key, item in resource.items()
                         if key in fields))
        return resource

    # Note(thread-safety): blocking function
    def add_datasource(self, item, deleted=False, update_db=True):
        req = self.make_datasource_dict(item)

        # check the request has valid information
        self.validate_create_datasource(req)
        if self.is_valid_service(req['name']):
            raise exception.DatasourceNameInUse(value=req['name'])

        new_id = req['id']
        LOG.debug("adding datasource %s", req['name'])
        if update_db:
            LOG.debug("updating db")
            try:
                # Note(thread-safety): blocking call
                datasource = datasources_db.add_datasource(
                    id_=req['id'],
                    name=req['name'],
                    driver=req['driver'],
                    config=req['config'],
                    description=req['description'],
                    enabled=req['enabled'])
            except db_exc.DBDuplicateEntry:
                raise exception.DatasourceNameInUse(value=req['name'])

        new_id = datasource['id']
        try:
            self.synchronize_datasources()
            # immediate synch policies on local PE if present
            # otherwise wait for regularly scheduled synch
            # TODO(dse2): use finer-grained method to synch specific policies
            engine = self.service_object('engine')
            if engine is not None:
                engine.synchronize_policies()
            # TODO(dse2): also broadcast to all PE nodes to synch
        except exception.DataServiceError:
            LOG.exception('the datasource service is already'
                          'created in the node')
        except Exception:
            LOG.exception(
                'Unexpected exception encountered while synchronizing new '
                'datasource %s.', req['name'])
            if update_db:
                # Note(thread-safety): blocking call
                datasources_db.delete_datasource(new_id)
            raise exception.DatasourceCreationError(value=req['name'])

        new_item = dict(item)
        new_item['id'] = new_id
        return self.make_datasource_dict(new_item)

    def validate_create_datasource(self, req):
        driver = req['driver']
        config = req['config'] or {}
        for loaded_driver in self.loaded_drivers.values():
            if loaded_driver['id'] == driver:
                specified_options = set(config.keys())
                valid_options = set(loaded_driver['config'].keys())
                # Check that all the specified options passed in are
                # valid configuration options that the driver exposes.
                invalid_options = specified_options - valid_options
                if invalid_options:
                    raise exception.InvalidDriverOption(
                        invalid_options=invalid_options)

                # check that all the required options are passed in
                required_options = set(
                    [k for k, v in loaded_driver['config'].items()
                     if v == constants.REQUIRED])
                missing_options = required_options - specified_options
                if missing_options:
                    missing_options = ', '.join(missing_options)
                    raise exception.MissingRequiredConfigOptions(
                        missing_options=missing_options)
                return loaded_driver

        # If we get here no datasource driver match was found.
        raise exception.InvalidDriver(driver=req)

    # Note (thread-safety): blocking function
    def create_datasource_service(self, datasource):
        """Create a new DataService on this node.

        :param name is the name of the service.  Must be unique across all
               services
        :param classPath is a string giving the path to the class name, e.g.
               congress.datasources.fake_datasource.FakeDataSource
        :param args is the list of arguments to give the DataService
               constructor
        :param type_ is the kind of service
        :param id_ is an optional parameter for specifying the uuid.
        """
        # get the driver info for the datasource
        ds_dict = self.make_datasource_dict(datasource)
        if not ds_dict['enabled']:
            LOG.info("datasource %s not enabled, skip loading",
                     ds_dict['name'])
            return

        driver_info = self.get_driver_info(ds_dict['driver'])
        # split class_path into module and class name
        class_path = driver_info['module']
        pieces = class_path.split(".")
        module_name = ".".join(pieces[:-1])
        class_name = pieces[-1]

        if ds_dict['config'] is None:
            args = {'ds_id': ds_dict['id']}
        else:
            args = dict(ds_dict['config'], ds_id=ds_dict['id'])
        kwargs = {'name': ds_dict['name'], 'args': args}
        LOG.info("creating service %s with class %s and args %s",
                 ds_dict['name'], module_name,
                 strutils.mask_password(kwargs, "****"))

        # import the module
        try:
            # Note(thread-safety): blocking call?
            module = importutils.import_module(module_name)
            service = getattr(module, class_name)(**kwargs)
        except Exception:
            msg = ("Error loading instance of module '%s'")
            LOG.exception(msg, class_path)
            raise exception.DataServiceError(msg % class_path)
        return service

    # Note(thread-safety): blocking function
    # FIXME(thread-safety): make sure unregister_service succeeds even if
    #   service already unregistered
    def delete_datasource(self, datasource, update_db=True):
        LOG.debug("Deleting %s datasource ", datasource['name'])
        datasource_id = datasource['id']
        session = db.get_session()
        with session.begin(subtransactions=True):
            if update_db:
                # Note(thread-safety): blocking call
                result = datasources_db.delete_datasource(
                    datasource_id, session)
                if not result:
                    raise exception.DatasourceNotFound(id=datasource_id)
                db_ds_table_data.delete_ds_table_data(ds_id=datasource_id)
            # Note(thread-safety): blocking call
            self.unregister_service(datasource['name'])


class DseNodeEndpoints (object):
    """Collection of RPC endpoints that the DseNode exposes on the bus.

       Must be a separate class since all public methods of a given
       class are assumed to be valid RPC endpoints.
    """

    def __init__(self, dsenode):
        self.node = dsenode

    # Note(ekcs): non-sequenced publish retained to simplify rollout of dse2
    #   to be replaced by handle_publish_sequenced
    def handle_publish(self, context, publisher, table, data):
        """Function called on the node when a publication is sent.

           Forwards the publication to all of the relevant services.
        """
        for s in self.node.table_subscribers(publisher, table):
            self.node.service_object(s).receive_data(
                publisher=publisher, table=table, data=data, is_snapshot=True)

    def handle_publish_sequenced(
            self, context, publisher, table, data, is_snapshot, seqnum):
        """Function called on the node when a publication is sent.

           Forwards the publication to all of the relevant services.
        """
        for s in self.node.table_subscribers(publisher, table):
            self.node.service_object(s).receive_data_sequenced(
                publisher=publisher, table=table, data=data, seqnum=seqnum,
                is_snapshot=is_snapshot)
