#
# Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
#
import logging
from collections import OrderedDict
import requests
from requests.exceptions import ConnectionError

import ConfigParser
import pprint
# always try to load simplejson first
# as we get better performance
try:
    import simplejson as json
except ImportError:
    import json
import time
import platform
import functools
import __main__ as main
import ssl
import re
import os
from urlparse import urlparse

from gen.vnc_api_client_gen import all_resource_type_tuples
from gen.resource_xsd import *
from gen.resource_client import *
from gen.generatedssuper import GeneratedsSuper

from utils import (
    OP_POST, OP_PUT, OP_GET, OP_DELETE, hdr_client_tenant,
    _obj_serializer_all, obj_type_to_vnc_class, getCertKeyCaBundle,
    AAA_MODE_VALID_VALUES, CamelCase, str_to_class)
from exceptions import (
    ServiceUnavailableError, NoIdError, PermissionDenied, OverQuota,
    RefsExistError, TimeOutError, BadRequest, HttpError,
    ResourceTypeUnknownError, RequestSizeError, AuthFailed)
import ssl_adapter

DEFAULT_LOG_DIR = "/var/tmp/contrail_vnc_lib"


def check_homepage(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self._srv_root_url:
            homepage = self._request(OP_GET, self._base_url,
                                     retry_on_error=False)
            self._parse_homepage(homepage)
        return func(self, *args, **kwargs)
    return wrapper


def get_object_class(res_type):
    cls_name = '%s' % (CamelCase(res_type))
    return str_to_class(cls_name, __name__)
# end get_object_class


def _read_cfg(cfg_parser, section, option, default):
    try:
        val = cfg_parser.get(section, option)
    except (AttributeError,
            ConfigParser.NoOptionError,
            ConfigParser.NoSectionError):
        val = default

    return val
# end _read_cfg


class CurlLogger(object):
    def __init__(self, log_file="/var/log/contrail/vnc-api.log"):
        if os.path.dirname(log_file):
            # absolute path to log file provided
            self.log_file = log_file
        else:
            # log file name provided
            self.log_file = os.path.join("/var/log/contrail", log_file)

        # make sure the log dir exists.
        if not os.path.exists(os.path.dirname(self.log_file)):
            try:
                os.makedirs(os.path.dirname(self.log_file))
            except OSError:
                # create logs in the tmp directory
                if not os.path.exists(DEFAULT_LOG_DIR):
                    os.makedirs(DEFAULT_LOG_DIR)
                self.log_file = os.path.join(DEFAULT_LOG_DIR,
                        os.path.basename(self.log_file))

        formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s',
                                      datefmt='%Y/%m/%d %H:%M:%S')
        self.curl_logger = logging.getLogger('log_curl')
        self.curl_logger.setLevel(logging.DEBUG)
        if os.path.exists(self.log_file):
            curl_log_handler = logging.FileHandler(self.log_file, mode='a')
        else:
            curl_log_handler = logging.FileHandler(self.log_file, mode='w')
        curl_log_handler.setFormatter(formatter)
        self.curl_logger.addHandler(curl_log_handler)
        self.pattern = re.compile(r'(.*-H "X-AUTH-TOKEN:)[0-9a-z]+(")')
    # end __init__

    def log(self, op, url, data=None, headers=None):
        if not headers:
            headers = {}
        op_str = {'get': 'GET', 'post': 'POST',
                  'delete': 'DELETE', 'put': 'PUT'}
        cmd_url = url
        cmd_hdr = None
        cmd_op = str(op_str[op])
        cmd_data = None
        header_list = [j + ":" + k for (j, k) in headers.items()]
        header_string = ''.join(['-H "' + str(i) + '" ' for i in header_list])
        cmd_hdr = re.sub(self.pattern, r'\1$TOKEN\2', header_string)
        if op == 'get':
            if data:
                query_string = "?" + "&".join([str(i) + "=" + str(j)
                                               for (i, j) in data.items()])
                cmd_url = url + query_string
        elif op == 'delete':
            pass
        else:
            cmd_data = str(data)
        if cmd_data:
            cmd = "curl -X %s %s -d '%s' %s" % (cmd_op, cmd_hdr,
                                                cmd_data, cmd_url)
        else:
            cmd = "curl -X %s %s %s" % (cmd_op, cmd_hdr, cmd_url)
        self.curl_logger.debug(cmd)
    # end log

    def log_response(self, resp):
        self.curl_logger.debug("RESP: %s %s %s",
            resp.status_code, resp.headers, resp.text)
    # end log_response
# end CurlLogger



class ActionUriDict(dict):
    """Action uri dictionary with operator([]) overloading to parse home page
       and populate the action_uri, if not populated already.
    """

    def __init__(self, vnc_api,  *args, **kwargs):
        dict.__init__(self, args, **kwargs)
        self.vnc_api = vnc_api

    def __getitem__(self, key):
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            homepage = self.vnc_api._request(
                OP_GET, self.vnc_api._base_url, retry_on_error=False)
            self.vnc_api._parse_homepage(homepage)
            return dict.__getitem__(self, key)


class ApiServerSession(object):
    def __init__(self, api_server_hosts, max_conns_per_pool,
            max_pools, logger=None):
        self.api_server_hosts = api_server_hosts
        self.max_conns_per_pool = max_conns_per_pool
        self.max_pools = max_pools
        self.logger = logger
        self.api_server_sessions = OrderedDict()
        self.active_session = (None, None)
        self.create()
    # end __init__

    def roundrobin(self):
        session_hosts = self.api_server_sessions.keys()
        if self.active_session[0] in session_hosts:
            last_used_index = session_hosts.index(self.active_session[0])
        else:
            # no active session start from the first host
            last_used_index = -1
        # use the next host in the list
        next_index = last_used_index + 1
        if next_index >= len(session_hosts):
            # reuse the first host from the list
            next_index = 0
        active_host = session_hosts[next_index]
        self.active_session = (active_host,
                               self.api_server_sessions[active_host])

    def create(self):
        for api_server_host in self.api_server_hosts:
            api_server_session = requests.Session()

            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.max_conns_per_pool,
                pool_maxsize=self.max_pools)
            ssladapter = ssl_adapter.SSLAdapter(ssl.PROTOCOL_SSLv23)
            ssladapter.init_poolmanager(
                connections=self.max_conns_per_pool,
                maxsize=self.max_pools)
            api_server_session.mount("http://", adapter)
            api_server_session.mount("https://", ssladapter)
            self.api_server_sessions.update(
                {api_server_host: api_server_session})
    # end create

    def get_url(self, url, api_server_host):
        parsed_url = urlparse(url)
        port = parsed_url.netloc.split(':')[-1]
        modified_url = parsed_url._replace(
            netloc=':'.join([api_server_host, port]))
        return modified_url.geturl()
    # end get_url

    def crud(self, method, url, *args, **kwargs):
        self.roundrobin()
        active_host, active_session = self.active_session
        if active_host and active_session:
            if active_host not in url:
                url = self.get_url(url, active_host)
            crud_method = getattr(active_session, '%s' % method)
            try:
                if self.logger:
                    data = kwargs.get('params',
                            kwargs.get('data', None))
                    headers = kwargs.get('headers', None)
                    self.logger.log(op=method, url=url,
                            data=data, headers=headers)
                result = crud_method(url, *args, **kwargs)
                if self.logger:
                    self.logger.log_response(result)
                return result
            except ConnectionError:
                self.active_session = (None, None)

        for host, session in self.api_server_sessions.items():
            if host not in url:
                url = self.get_url(url, host)
            crud_method = getattr(session, '%s' % method)
            try:
                if self.logger:
                    data = kwargs.get('params',
                            kwargs.get('data', None))
                    headers = kwargs.get('headers', None)
                    self.logger.log(op=method, url=url,
                            data=data, headers=headers)
                result = crud_method(url, *args, **kwargs)
                if self.logger:
                    self.logger.log_response(result)
                self.active_session = (host, session)
                return result
            except ConnectionError:
                continue
        raise ConnectionError
    # end crud

    def get(self, url, *args, **kwargs):
        return self.crud('get', url, *args, **kwargs)
    # end get

    def post(self, url, *args, **kwargs):
        return self.crud('post', url, *args, **kwargs)
    # end post

    def put(self, url, *args, **kwargs):
        return self.crud('put', url, *args, **kwargs)
    # end put

    def delete(self, url, *args, **kwargs):
        return self.crud('delete', url, *args, **kwargs)
    # end delete


class VncApi(object):
    _DEFAULT_WEB_SERVER = "127.0.0.1"

    hostname = platform.node()
    _DEFAULT_HEADERS = {
        'Content-type': 'application/json; charset="UTF-8"',
        'X-Contrail-Useragent': '%s:%s' % (hostname,
                                           getattr(main, '__file__', '')),
    }

    _NOAUTH_AUTHN_STRATEGY = 'noauth'
    _KEYSTONE_AUTHN_STRATEGY = 'keystone'
    _DEFAULT_AUTHN_STRATEGY = _KEYSTONE_AUTHN_STRATEGY
    AUTHN_SUPPORTED_STRATEGIES = [_NOAUTH_AUTHN_STRATEGY,
                                  _KEYSTONE_AUTHN_STRATEGY]
    _DEFAULT_AUTHN_HEADERS = _DEFAULT_HEADERS
    _DEFAULT_AUTHN_PROTOCOL = "http"
    _DEFAULT_AUTHN_SERVER = _DEFAULT_WEB_SERVER
    _DEFAULT_AUTHN_PORT = 35357
    _DEFAULT_AUTHN_URL = None
    _DEFAULT_AUTHN_USER = ""
    _DEFAULT_AUTHN_PASSWORD = ""
    _DEFAULT_AUTHN_TENANT = 'default-tenant'
    _DEFAULT_DOMAIN_ID = "default"

    # Keystone and and vnc-api SSL support
    # contrail-api will remain to be on http
    # with LB (haproxy/F5/nginx..etc) configured for
    # ssl termination on port 8082(default contrail-api port)
    _DEFAULT_API_SERVER_CONNECT = "http"
    _DEFAULT_API_SERVER_SSL_CONNECT = "https"
    _DEFAULT_KS_CERT_BUNDLE = "keystonecertbundle.pem"
    _DEFAULT_API_CERT_BUNDLE = "apiservercertbundle.pem"

    # Connection to api-server through Quantum
    _DEFAULT_WEB_PORT = 8082
    _DEFAULT_BASE_URL = "/"

    # The number of items beyond which instead of GET /<collection>
    # a POST /list-bulk-collection is issued
    POST_FOR_LIST_THRESHOLD = 25

    # Number of pools and number of pool per conn to api-server
    _DEFAULT_MAX_POOLS = 100
    _DEFAULT_MAX_CONNS_PER_POOL = 100

    # Defined in Sandesh common headers but not importable in vnc_api lib
    _SECURITY_OBJECT_TYPES = [
        ApplicationPolicySet.object_type,
        FirewallPolicy.object_type,
        FirewallRule.object_type,
        ServiceGroup.object_type,
        AddressGroup.object_type,
    ]
    _POLICY_MANAGEMENT_NAME_FOR_SECURITY_DRAFT = 'draft-policy-management'

    def __init__(self, username=None, password=None, tenant_name=None,
                 api_server_host=None, api_server_port=None,
                 api_server_url=None, conf_file=None, user_info=None,
                 auth_token=None, auth_host=None, auth_port=None,
                 auth_protocol=None, auth_url=None, auth_type=None,
                 wait_for_connect=False, api_server_use_ssl=None,
                 domain_name=None, exclude_hrefs=None, auth_token_url=None,
                 apicertfile=None, apikeyfile=None, apicafile=None,
                 kscertfile=None, kskeyfile=None, kscafile=None,
                 apiinsecure=None, ksinsecure=None):
        # TODO allow for username/password to be present in creds file

        self._obj_serializer = self._obj_serializer_diff
        for object_type, resource_type in all_resource_type_tuples:
            for oper_str in ('_create', '_read', '_update', '_delete',
                             's_list', '_get_default_id', '_read_draft'):
                if (oper_str == '_read_draft' and
                        object_type not in self._SECURITY_OBJECT_TYPES):
                    continue
                method = getattr(self, '_object%s' % oper_str)
                bound_method = functools.partial(method, resource_type)
                functools.update_wrapper(bound_method, method)
                if oper_str == '_get_default_id':
                    setattr(self, 'get_default_%s_id' % (object_type),
                            bound_method)
                else:
                    setattr(self, '%s%s' % (object_type, oper_str),
                            bound_method)

        cfg_parser = ConfigParser.ConfigParser()
        try:
            cfg_parser.read(conf_file or
                            "/etc/contrail/vnc_api_lib.ini")
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.warn("Exception: %s", str(e))

        self._api_connect_protocol = VncApi._DEFAULT_API_SERVER_CONNECT
        # API server SSL Support
        if api_server_use_ssl is None:
            api_server_use_ssl = _read_cfg(cfg_parser, 'global', 'use_ssl', False)
        use_ssl = (str(api_server_use_ssl).lower() == 'true')
        if use_ssl:
            self._api_connect_protocol = VncApi._DEFAULT_API_SERVER_SSL_CONNECT

        if not api_server_host:
            self._web_hosts = _read_cfg(cfg_parser, 'global', 'WEB_SERVER',
                                        self._DEFAULT_WEB_SERVER).split(',')
        elif isinstance(api_server_host, list):
            self._web_hosts = api_server_host
        else:
            self._web_hosts = [api_server_host]
        self._web_host = self._web_hosts[0]

        # contrail-api SSL support
        if apiinsecure is not None:
            self._apiinsecure = apiinsecure
        else:
            try:
                self._apiinsecure = cfg_parser.getboolean('global', 'insecure')
            except (AttributeError,
                    ValueError,
                    ConfigParser.NoOptionError,
                    ConfigParser.NoSectionError):
                self._apiinsecure = False
        apicertfile = (apicertfile or
                       _read_cfg(cfg_parser, 'global', 'certfile', ''))
        apikeyfile = (apikeyfile or
                      _read_cfg(cfg_parser, 'global', 'keyfile', ''))
        apicafile = (apicafile or
                     _read_cfg(cfg_parser, 'global', 'cafile', ''))

        self._use_api_certs = False
        if apicafile and use_ssl:
            certs = [apicafile]
            if apikeyfile and apicertfile:
                certs = [apicertfile, apikeyfile, apicafile]
            apicertbundle = os.path.join(
                '/tmp', self._web_host.replace('.', '_'),
                VncApi._DEFAULT_API_CERT_BUNDLE)
            self._apicertbundle = getCertKeyCaBundle(apicertbundle,
                                                     certs)
            self._use_api_certs = True

        self._authn_strategy = auth_type or \
            _read_cfg(cfg_parser, 'auth', 'AUTHN_TYPE',
                      self._DEFAULT_AUTHN_STRATEGY)
        if self._authn_strategy not in VncApi.AUTHN_SUPPORTED_STRATEGIES:
            raise NotImplementedError("The authentication strategy '%s' is "
                                      "not supported by the VNC API lib" %
                                      self._authn_strategy)

        self._user_info = user_info

        if self._authn_strategy == VncApi._KEYSTONE_AUTHN_STRATEGY:
            self._authn_protocol = auth_protocol or \
                _read_cfg(cfg_parser, 'auth', 'AUTHN_PROTOCOL',
                          self._DEFAULT_AUTHN_PROTOCOL)
            self._authn_server = auth_host or \
                _read_cfg(cfg_parser, 'auth', 'AUTHN_SERVER',
                          self._DEFAULT_AUTHN_SERVER)
            self._authn_port = auth_port or \
                _read_cfg(cfg_parser, 'auth', 'AUTHN_PORT',
                          self._DEFAULT_AUTHN_PORT)
            self._authn_url = auth_url or \
                _read_cfg(cfg_parser, 'auth', 'AUTHN_URL',
                          self._DEFAULT_AUTHN_URL)
            self._username = username or \
                _read_cfg(cfg_parser, 'auth', 'AUTHN_USER',
                          self._DEFAULT_AUTHN_USER)
            self._password = password or \
                _read_cfg(cfg_parser, 'auth', 'AUTHN_PASSWORD',
                          self._DEFAULT_AUTHN_PASSWORD)
            self._tenant_name = tenant_name or \
                _read_cfg(cfg_parser, 'auth', 'AUTHN_TENANT',
                          self._DEFAULT_AUTHN_TENANT)
            self._domain_name = domain_name or \
                _read_cfg(cfg_parser, 'auth', 'AUTHN_DOMAIN',
                          self._DEFAULT_DOMAIN_ID)
            self._authn_token_url = auth_token_url or \
                _read_cfg(cfg_parser, 'auth', 'AUTHN_TOKEN_URL', None)

            # keystone SSL support
            if ksinsecure is not None:
                self._ksinsecure = ksinsecure
            else:
                try:
                    self._ksinsecure = cfg_parser.getboolean('auth', 'insecure')
                except (AttributeError,
                        ValueError,
                        ConfigParser.NoOptionError,
                        ConfigParser.NoSectionError):
                    self._ksinsecure = False
            kscertfile = (kscertfile or
                          _read_cfg(cfg_parser, 'auth', 'certfile', ''))
            kskeyfile = (kskeyfile or
                         _read_cfg(cfg_parser, 'auth', 'keyfile', ''))
            kscafile = (kscafile or
                        _read_cfg(cfg_parser, 'auth', 'cafile', ''))

            self._use_ks_certs = False
            if kscafile and self._authn_protocol == 'https':
                certs = [kscafile]
                if kskeyfile and kscertfile:
                    certs = [kscertfile, kskeyfile, kscafile]
                kscertbundle = os.path.join(
                    '/tmp', self._web_host.replace('.', '_'),
                    VncApi._DEFAULT_KS_CERT_BUNDLE)
                self._kscertbundle = getCertKeyCaBundle(kscertbundle,
                                                        certs)
                self._use_ks_certs = True

            self._v2_authn_body = \
                '{"auth":{"passwordCredentials":{' + \
                '"username": "%s",' % (self._username) + \
                ' "password": "%s"},' % (self._password) + \
                ' "tenantName":"%s"}}' % (self._tenant_name)
            self._v3_authn_body = \
                '{"auth":{"identity":{' + \
                '"methods": ["password"],' + \
                ' "password":{' + \
                ' "user":{' + \
                ' "name": "%s",' % (self._username) + \
                ' "domain": { "name": "%s" },' % (self._domain_name) +\
                ' "password": "%s"' % (self._password) + \
                '}' + \
                '}' + \
                '},' + \
                ' "scope":{' + \
                ' "project":{' + \
                ' "domain": { "name": "%s" },' % (self._domain_name) +\
                ' "name": "%s"' % (self._tenant_name) + \
                '}' + \
                '}' + \
                '}' + \
                '}'
            if not self._authn_url:
                self._discover()
            elif 'v2' in self._authn_url:
                self._authn_body = self._v2_authn_body
            else:
                self._authn_body = self._v3_authn_body

        if not api_server_port:
            self._web_port = _read_cfg(cfg_parser, 'global', 'WEB_PORT',
                                       self._DEFAULT_WEB_PORT)
        else:
            self._web_port = api_server_port

        self._max_pools = int(_read_cfg(
            cfg_parser, 'global', 'MAX_POOLS',
            self._DEFAULT_MAX_POOLS))
        self._max_conns_per_pool = int(_read_cfg(
            cfg_parser, 'global', 'MAX_CONNS_PER_POOL',
            self._DEFAULT_MAX_CONNS_PER_POOL))

        self.curl_logger = None
        if _read_cfg(cfg_parser, 'global', 'curl_log', False):
            self.curl_logger = CurlLogger(
                    _read_cfg(cfg_parser, 'global', 'curl_log', False))

        # Where client's view of world begins
        if not api_server_url:
            self._base_url = _read_cfg(cfg_parser, 'global', 'BASE_URL',
                                       self._DEFAULT_BASE_URL)
        else:
            self._base_url = api_server_url

        # Where server says its root is when _base_url is fetched
        self._srv_root_url = None

        # Type-independent actions offered by server
        self._action_uri = ActionUriDict(self)

        self._headers = self._DEFAULT_HEADERS.copy()
        if self._authn_strategy == VncApi._KEYSTONE_AUTHN_STRATEGY:
            self._headers[hdr_client_tenant()] = self._tenant_name

        self._auth_token_input = False
        self._auth_token = None

        if auth_token:
            self._auth_token = auth_token
            self._auth_token_input = True
            self._headers['X-AUTH-TOKEN'] = self._auth_token

        # user information for quantum
        if self._user_info:
            if 'user_id' in self._user_info:
                self._headers['X-API-USER-ID'] = self._user_info['user_id']
            if 'user' in self._user_info:
                self._headers['X-API-USER'] = self._user_info['user']
            if 'role' in self._user_info:
                self.set_user_roles([self._user_info['role']])

        self._exclude_hrefs = exclude_hrefs

        self._create_api_server_session()

        retry_count = 6
        while retry_count:
            try:
                homepage = self._request(OP_GET, self._base_url,
                                         retry_on_error=False)
                self._parse_homepage(homepage)
            except ServiceUnavailableError as e:
                logger = logging.getLogger(__name__)
                logger.warn("Exception: %s", str(e))
                if wait_for_connect:
                    # Retry connect infinitely when http retcode 503
                    continue
                elif retry_count:
                    # Retry connect 60 times when http retcode 503
                    retry_count -= 1
                    time.sleep(1)
            else:
                # connected successfully
                break
    # end __init__

    @check_homepage
    def _object_create(self, res_type, obj):
        obj_cls = obj_type_to_vnc_class(res_type, __name__)

        obj._pending_field_updates |= obj._pending_ref_updates
        obj._pending_ref_updates = set([])
        # Ignore fields with None value in json representation
        # encode props + refs in object body
        obj_json_param = json.dumps(obj, default=self._obj_serializer)

        json_body = '{"%s":%s}' % (res_type, obj_json_param)
        content = self._request_server(
            OP_POST, obj_cls.create_uri, data=json_body)

        obj_dict = json.loads(content)[res_type]
        obj.uuid = obj_dict['uuid']
        obj.fq_name = obj_dict['fq_name']
        if 'parent_type' in obj_dict:
            obj.parent_type = obj_dict['parent_type']
        if 'parent_uuid' in obj_dict:
            obj.parent_uuid = obj_dict['parent_uuid']

        obj.set_server_conn(self)

        # encode any prop-<list|map> operations and
        # POST on /prop-collection-update
        prop_coll_body = {'uuid': obj.uuid,
                          'updates': []}

        operations = []
        for prop_name in obj._pending_field_list_updates:
            operations.extend(obj._pending_field_list_updates[prop_name])
        for prop_name in obj._pending_field_map_updates:
            operations.extend(obj._pending_field_map_updates[prop_name])

        for oper, elem_val, elem_pos in operations:
            if isinstance(elem_val, GeneratedsSuper):
                serialized_elem_value = elem_val.exportDict('')
            else:
                serialized_elem_value = elem_val

            prop_coll_body['updates'].append(
                {'field': prop_name, 'operation': oper,
                 'value': serialized_elem_value, 'position': elem_pos})

        # all pending fields picked up
        obj.clear_pending_updates()

        if prop_coll_body['updates']:
            prop_coll_json = json.dumps(prop_coll_body)
            self._request_server(
                OP_POST, self._action_uri['prop-collection-update'],
                data=prop_coll_json)

        return obj.uuid
    # end _object_create

    @check_homepage
    def _object_read(self, res_type, fq_name=None, fq_name_str=None,
                     id=None, ifmap_id=None, fields=None,
                     exclude_back_refs=True, exclude_children=True):
        obj_cls = obj_type_to_vnc_class(res_type, __name__)

        (args_ok, result) = self._read_args_to_id(
            res_type, fq_name, fq_name_str, id, ifmap_id)
        if not args_ok:
            return result

        id = result
        uri = obj_cls.resource_uri_base[res_type] + '/' + id

        fields = set(fields or [])
        if fields:
            # filter fields with only known attributes
            fields = (fields & (
                obj_cls.prop_fields |
                obj_cls.children_fields |
                obj_cls.ref_fields |
                obj_cls.backref_fields)
            )
            query_params = {'fields': ','.join(f for f in fields)}
        else:
            query_params = dict()
            if exclude_back_refs is True:
                query_params['exclude_back_refs'] = True
            if exclude_children is True:
                query_params['exclude_children'] = True

        if self._exclude_hrefs is not None:
            query_params['exclude_hrefs'] = True

        response = self._request_server(OP_GET, uri, query_params)

        obj_dict = response[res_type]
        # if requested child/backref fields are not in the result, that means
        # resource does not have child/backref of that type. Set it to None to
        # prevent VNC client lib to call again VNC API when user uses the get
        # child/backref method on that type in the 'resource_client' file
        [obj_dict.setdefault(field, None) for field
         in fields & (obj_cls.backref_fields | obj_cls.children_fields)]
        obj = obj_cls.from_dict(**obj_dict)
        obj.clear_pending_updates()
        obj.set_server_conn(self)

        return obj
    # end _object_read

    @check_homepage
    def _object_read_draft(self, res_type, fq_name=None, fq_name_str=None,
                           id=None, fields=None):
        if not (fq_name or fq_name_str or id):
            return ("To get draft version of a security resource at least "
                    "fully qualified name or UUID is required")

        if not (fq_name or fq_name_str) and id:
            fq_name = self.id_to_fq_name(id)
        id = None

        if not fq_name and fq_name_str:
            fq_name = fq_name_str.split(':')
            fq_name_str = None

        draft_fq_name = list(fq_name)
        if self._POLICY_MANAGEMENT_NAME_FOR_SECURITY_DRAFT not in fq_name:
            if len(fq_name) == 2:
                draft_fq_name = [
                    self._POLICY_MANAGEMENT_NAME_FOR_SECURITY_DRAFT,
                    draft_fq_name[-1],
                ]
            else:
                draft_fq_name.insert(
                    -1, self._POLICY_MANAGEMENT_NAME_FOR_SECURITY_DRAFT)

        return self._object_read(res_type, fq_name=draft_fq_name,
                                 fields=fields)

    @check_homepage
    def _object_update(self, res_type, obj):
        obj_cls = obj_type_to_vnc_class(res_type, __name__)

        # Read in uuid from api-server if not specified in obj
        if not obj.uuid:
            obj.uuid = self.fq_name_to_id(res_type, obj.get_fq_name())

        # Generate PUT on object only if some attr was modified
        content = None
        if obj.get_pending_updates():
            # Ignore fields with None value in json representation
            obj_json_param = json.dumps(obj, default=self._obj_serializer)
            if obj_json_param:
                json_body = '{"%s":%s}' % (res_type, obj_json_param)
                uri = obj_cls.resource_uri_base[res_type] + '/' + obj.uuid
                content = self._request_server(
                    OP_PUT, uri, data=json_body)

        # Generate POST on /prop-collection-update if needed/pending
        prop_coll_body = {'uuid': obj.uuid,
                          'updates': []}

        operations = []
        for prop_name in obj._pending_field_list_updates:
            operations.extend(obj._pending_field_list_updates[prop_name])
        for prop_name in obj._pending_field_map_updates:
            operations.extend(obj._pending_field_map_updates[prop_name])

        for oper, elem_val, elem_pos in operations:
            if isinstance(elem_val, GeneratedsSuper):
                serialized_elem_value = elem_val.exportDict('')
            else:
                serialized_elem_value = elem_val

            prop_coll_body['updates'].append(
                {'field': prop_name, 'operation': oper,
                 'value': serialized_elem_value, 'position': elem_pos})

        if prop_coll_body['updates']:
            prop_coll_json = json.dumps(prop_coll_body)
            self._request_server(
                OP_POST, self._action_uri['prop-collection-update'],
                data=prop_coll_json)

        # Generate POST on /ref-update if needed/pending
        for ref_name in obj._pending_ref_updates:
            ref_orig = set(
                [(x.get('uuid'), tuple(x.get('to', [])), x.get('attr'))
                 for x in getattr(obj, '_original_' + ref_name, [])])
            ref_new = set(
                [(x.get('uuid'), tuple(x.get('to', [])), x.get('attr'))
                 for x in getattr(obj, ref_name, [])])
            for ref in ref_orig - ref_new:
                self.ref_update(
                    res_type, obj.uuid, ref_name, ref[0], list(ref[1]),
                    'DELETE')
            for ref in ref_new - ref_orig:
                self.ref_update(
                    res_type, obj.uuid, ref_name, ref[0], list(ref[1]),
                    'ADD', ref[2])
        obj.clear_pending_updates()

        return content
    # end _object_update

    @check_homepage
    def _objects_list(self, res_type, parent_id=None, parent_fq_name=None,
                      obj_uuids=None, back_ref_id=None, fields=None,
                      detail=False, count=False, filters=None, shared=False,
                      fq_names=None):
        return self.resource_list(
            res_type, parent_id=parent_id, parent_fq_name=parent_fq_name,
            back_ref_id=back_ref_id, obj_uuids=obj_uuids, fields=fields,
            detail=detail, count=count, filters=filters, shared=shared,
            fq_names=fq_names)
    # end _objects_list

    @check_homepage
    def _object_delete(self, res_type, fq_name=None, id=None, ifmap_id=None):
        obj_cls = obj_type_to_vnc_class(res_type, __name__)

        (args_ok, result) = self._read_args_to_id(
            res_type=res_type, fq_name=fq_name, id=id, ifmap_id=ifmap_id)
        if not args_ok:
            return result

        id = result
        uri = obj_cls.resource_uri_base[res_type] + '/' + id

        self._request_server(OP_DELETE, uri)
    # end _object_delete

    def _object_get_default_id(self, res_type):
        obj_cls = obj_type_to_vnc_class(res_type, __name__)

        return self.fq_name_to_id(res_type, obj_cls().get_fq_name())
    # end _object_get_default_id

    def _obj_serializer_diff(self, obj):
        if hasattr(obj, 'serialize_to_json'):
            try:
                return obj.serialize_to_json(obj.get_pending_updates())
            except AttributeError:
                # Serialize all fields in xsd types
                return obj.serialize_to_json()
        else:
            return dict((k, v) for k, v in obj.__dict__.iteritems())
    # end _obj_serializer_diff

    def _create_api_server_session(self):
        self._api_server_session = ApiServerSession(
            self._web_hosts, self._max_conns_per_pool,
            self._max_pools, self.curl_logger)
    # end _create_api_server_session

    def _discover(self):
        """Discover the authn_url when not specified"""
        try:
            # Try keystone v3
            self._authn_url = '/v3/auth/tokens'
            self._authn_body = self._v3_authn_body
            self._authenticate()
        except RuntimeError:
            # Use keystone v2
            self._authn_url = '/v2.0/tokens'
            self._authn_body = self._v2_authn_body
    # end _discover

    # Authenticate with configured service
    def _authenticate(self, response=None, headers=None):
        if self._authn_strategy == VncApi._NOAUTH_AUTHN_STRATEGY:
            return headers

        elif self._authn_strategy == VncApi._KEYSTONE_AUTHN_STRATEGY:
            if self._authn_token_url:
                url = self._authn_token_url
            else:
                url = "%s://%s:%s%s" % (
                    self._authn_protocol,
                    self._authn_server,
                    self._authn_port,
                    self._authn_url,
                )
            new_headers = headers or {}
            try:
                if self._ksinsecure:
                    response = requests.post(
                        url,
                        data=self._authn_body,
                        headers=self._DEFAULT_AUTHN_HEADERS,
                        verify=False,
                    )
                elif not self._ksinsecure and self._use_ks_certs:
                    response = requests.post(
                        url,
                        data=self._authn_body,
                        headers=self._DEFAULT_AUTHN_HEADERS,
                        verify=self._kscertbundle,
                    )
                else:
                    response = requests.post(
                        url,
                        data=self._authn_body,
                        headers=self._DEFAULT_AUTHN_HEADERS,
                    )
            except Exception as e:
                errmsg = ('Unable to connect to keystone (%s) for authentication. '
                    'Exception %s' % (url, e))
                raise RuntimeError(errmsg)

            if (response.status_code == 200) or (response.status_code == 201):
                # plan is to re-issue original request with new token
                if 'v2' in self._authn_url:
                    authn_content = json.loads(response.text)
                    self._auth_token = authn_content['access']['token']['id']
                else:
                    self._auth_token = response.headers['x-subject-token']
                new_headers['X-AUTH-TOKEN'] = self._auth_token
                return new_headers
            else:
                raise RuntimeError('Authentication Failure')
    # end _authenticate

    def _http_get(self, uri, headers=None, query_params=None):
        url = "%s://%s:%s%s" % (self._api_connect_protocol,
                                self._web_host, self._web_port, uri)
        if self._apiinsecure:
            response = self._api_server_session.get(
                url, headers=headers, params=query_params, verify=False)
        elif not self._apiinsecure and self._use_api_certs:
            response = self._api_server_session.get(
                url, headers=headers, params=query_params,
                verify=self._apicertbundle)
        else:
            response = self._api_server_session.get(
                url, headers=headers, params=query_params)
        # print 'Sending Request URL: ' + pformat(url)
        # print '                Headers: ' + pformat(headers)
        # print '                QParams: ' + pformat(query_params)
        # response = self._api_server_session.get(url, headers = headers,
        #                                        params = query_params)
        # print 'Received Response: ' + pformat(response.text)
        return (response.status_code, response.text)
    # end _http_get

    def _http_post(self, uri, body, headers):
        url = "%s://%s:%s%s" % (self._api_connect_protocol,
                                self._web_host, self._web_port, uri)
        if self._apiinsecure:
            response = self._api_server_session.post(
                url, data=body, headers=headers, verify=False)
        elif not self._apiinsecure and self._use_api_certs:
            response = self._api_server_session.post(
                url, data=body, headers=headers,
                verify=self._apicertbundle)
        else:
            response = self._api_server_session.post(
                url, data=body, headers=headers)
        return (response.status_code, response.text)
    # end _http_post

    def _http_delete(self, uri, body, headers):
        url = "%s://%s:%s%s" % (self._api_connect_protocol,
                                self._web_host, self._web_port, uri)
        if self._apiinsecure:
            response = self._api_server_session.delete(
                url, data=body, headers=headers, verify=False)
        elif not self._apiinsecure and self._use_api_certs:
            response = self._api_server_session.delete(
                url, data=body, headers=headers,
                verify=self._apicertbundle)
        else:
            response = self._api_server_session.delete(
                url, data=body, headers=headers)
        return (response.status_code, response.text)
    # end _http_delete

    def _http_put(self, uri, body, headers):
        url = "%s://%s:%s%s" % (self._api_connect_protocol,
                                self._web_host, self._web_port, uri)
        if self._apiinsecure:
            response = self._api_server_session.put(
                url, data=body, headers=headers, verify=False)
        elif not self._apiinsecure and self._use_api_certs:
            response = self._api_server_session.put(
                url, data=body, headers=headers,
                verify=self._apicertbundle)
        else:
            response = self._api_server_session.put(
                url, data=body, headers=headers)
        return (response.status_code, response.text)
    # end _http_delete

    def _parse_homepage(self, py_obj):
        srv_root_url = py_obj['href']
        self._srv_root_url = srv_root_url

        for link in py_obj['links']:
            # strip base from *_url to get *_uri
            uri = link['link']['href'].replace(srv_root_url, '')
            if link['link']['rel'] == 'collection':
                cls = obj_type_to_vnc_class(link['link']['name'],
                                            __name__)
                if not cls:
                    continue
                cls.create_uri = uri
            elif link['link']['rel'] == 'resource-base':
                cls = obj_type_to_vnc_class(link['link']['name'],
                                            __name__)
                if not cls:
                    continue
                resource_type = link['link']['name']
                cls.resource_uri_base[resource_type] = uri
            elif link['link']['rel'] == 'action':
                act_type = link['link']['name']
                self._action_uri[act_type] = uri
    # end _parse_homepage

    def _find_url(self, json_body, resource_name):
        rname = unicode(resource_name)
        py_obj = json.loads(json_body)
        pprint.pprint(py_obj)
        for link in py_obj['links']:
            if link['link']['name'] == rname:
                return link['link']['href']

        return None
    # end _find_url

    def _read_args_to_id(self, res_type, fq_name=None, fq_name_str=None,
                         id=None, ifmap_id=None):
        arg_count = ((fq_name is not None) + (fq_name_str is not None) +
                     (id is not None) + (ifmap_id is not None))

        if (arg_count == 0):
            return (False, "at least one of the arguments has to be provided")
        elif (arg_count > 1):
            return (False, "only one of the arguments should be provided")

        if id:
            return (True, id)
        if fq_name:
            return (True, self.fq_name_to_id(res_type, fq_name))
        if fq_name_str:
            return (True, self.fq_name_to_id(res_type, fq_name_str.split(':')))
        if ifmap_id:
            return (False, "ifmap_id is no longer supported")
    # end _read_args_to_id

    def _request_server(self, op, url, data=None, retry_on_error=True,
                        retry_after_authn=False, retry_count=30):
        if not self._srv_root_url:
            raise ConnectionError("Unable to retrive the api server root url.")

        return self._request(
            op, url, data=data, retry_on_error=retry_on_error,
            retry_after_authn=retry_after_authn, retry_count=retry_count)
    # end _request_server

    def _request(self, op, url, data=None, retry_on_error=True,
                 retry_after_authn=False, retry_count=30):
        retried = 0
        while True:
            headers = self._headers
            user_token = headers.pop('X-USER-TOKEN', None)
            if user_token:
                headers = self._headers.copy()
                headers['X-AUTH-TOKEN'] = user_token
                retry_after_authn = True
            try:
                if (op == OP_GET):
                    (status, content) = self._http_get(
                        url, headers=headers, query_params=data)
                    if status == 200:
                        content = json.loads(content)
                elif (op == OP_POST):
                    (status, content) = self._http_post(
                        url, body=data, headers=headers)
                elif (op == OP_DELETE):
                    (status, content) = self._http_delete(
                        url, body=data, headers=headers)
                elif (op == OP_PUT):
                    (status, content) = self._http_put(
                        url, body=data, headers=headers)
                else:
                    raise ValueError
            except ConnectionError:
                if (not retry_on_error or not retry_count):
                    raise ConnectionError

                time.sleep(1)
                self._create_api_server_session()
                retry_count -= 1
                continue

            if status in [200, 202]:
                return content

            # Exception Response, see if it can be resolved
            if ((status == 401) and (not self._auth_token_input) and
                    (not retry_after_authn)):
                self._headers = self._authenticate(content, self._headers)
                # Recursive call after authentication (max 1 level)
                content = self._request(
                    op, url, data=data, retry_after_authn=True)

                return content
            elif status == 404:
                raise NoIdError('Error: oper %s url %s body %s response %s'
                                % (op, url, data, content))
            elif status == 403:
                raise PermissionDenied(content)
            elif status == 412:
                raise OverQuota(content)
            elif status == 409:
                raise RefsExistError(content)
            elif status == 413:
                raise RequestSizeError(content)
            elif status == 504:
                # Request sent to API server, but no response came within 50s
                raise TimeOutError('Gateway Timeout 504')
            elif status in [502, 503]:
                # 502: API server died after accepting request, so retry
                # 503: no API server available even before sending the request
                retried += 1
                if retried >= retry_count:
                    raise ServiceUnavailableError(
                        'Service Unavailable Timeout %d' % status)

                time.sleep(1)
                continue
            elif status == 400:
                raise BadRequest(status, content)
            elif status == 401:
                raise AuthFailed(status, content)
            else:  # Unknown Error
                raise HttpError(status, content)
        # end while True

    # end _request_server

    def _prop_collection_post(self, obj_uuid, obj_field,
                              oper, value, position):
        uri = self._action_uri['prop-collection-update']
        if isinstance(value, GeneratedsSuper):
            serialized_value = value.exportDict('')
        else:
            serialized_value = value

        oper_param = {'field': obj_field,
                      'operation': oper,
                      'value': serialized_value}
        if position:
            oper_param['position'] = position
        dict_body = {'uuid': obj_uuid, 'updates': [oper_param]}
        return self._request_server(
            OP_POST, uri, data=json.dumps(dict_body))
    # end _prop_collection_post

    def _prop_collection_get(self, obj_uuid, obj_field, position):
        uri = self._action_uri['prop-collection-get']
        query_params = {'uuid': obj_uuid, 'fields': obj_field}
        if position:
            query_params['position'] = position

        content = self._request_server(
            OP_GET, uri, data=query_params)

        return content[obj_field]
    # end _prop_collection_get

    def _prop_map_get_elem_key(self, id, obj_field, elem):
        _, res_type = self.id_to_fq_name_type(id)
        obj_class = obj_type_to_vnc_class(res_type, __name__)

        key_name = obj_class.prop_map_field_key_names[obj_field]
        if isinstance(elem, GeneratedsSuper):
            return getattr(elem, key_name)

        return elem[key_name]
    # end _prop_map_get_elem_key

    @check_homepage
    def prop_list_add_element(self, obj_uuid, obj_field, value, position=None):
        return self._prop_collection_post(
            obj_uuid, obj_field, 'add', value, position)
    # end prop_list_add_element

    @check_homepage
    def prop_list_modify_element(self, obj_uuid, obj_field, value, position):
        return self._prop_collection_post(
            obj_uuid, obj_field, 'modify', value, position)
    # end prop_list_modify_element

    @check_homepage
    def prop_list_delete_element(self, obj_uuid, obj_field, position):
        return self._prop_collection_post(
            obj_uuid, obj_field, 'delete', None, position)
    # end prop_list_delete_element

    @check_homepage
    def prop_list_get(self, obj_uuid, obj_field, position=None):
        return self._prop_collection_get(obj_uuid, obj_field, position)
    # end prop_list_get

    @check_homepage
    def prop_map_set_element(self, obj_uuid, obj_field, value):
        position = self._prop_map_get_elem_key(obj_uuid, obj_field, value)
        return self._prop_collection_post(
            obj_uuid, obj_field, 'set', value, position)
    # end prop_map_set_element

    @check_homepage
    def prop_map_delete_element(self, obj_uuid, obj_field, position):
        return self._prop_collection_post(
            obj_uuid, obj_field, 'delete', None, position)
    # end prop_map_delete_element

    @check_homepage
    def prop_map_get(self, obj_uuid, obj_field, position=None):
        return self._prop_collection_get(obj_uuid, obj_field, position)
    # end prop_list_get

    @check_homepage
    def execute_job(self, job_template_fq_name=None, job_template_id=None,
                    job_input=None, device_list=None):
        if job_template_fq_name is None and job_template_id is None:
            raise ValueError(
                "Either job_template_fq_name or job_template_id must be "\
                "specified with valid value")

        body = None
        if job_template_fq_name:
            body = { 'job_template_fq_name': job_template_fq_name }
        else:
            body = { 'job_template_id': job_template_id }

        body['input'] = job_input or {}
        if device_list:
            body['params'] = { 'device_list': device_list }

        json_body = json.dumps(body)
        uri = self._action_uri['execute-job']
        content = self._request_server(OP_POST, uri, data=json_body)
        return json.loads(content)
    # end execute_job

    @check_homepage
    def ref_update(self, obj_type, obj_uuid, ref_type, ref_uuid,
                   ref_fq_name, operation, attr=None):
        if ref_type.endswith(('_refs', '-refs')):
            ref_type = ref_type[:-5].replace('_', '-')
        json_body = json.dumps({'type': obj_type, 'uuid': obj_uuid,
                                'ref-type': ref_type, 'ref-uuid': ref_uuid,
                                'ref-fq-name': ref_fq_name,
                                'operation': operation, 'attr': attr},
                               default=self._obj_serializer_diff)
        uri = self._action_uri['ref-update']
        try:
            content = self._request_server(OP_POST, uri, data=json_body)
        except HttpError as he:
            if he.status_code == 404:
                return None
            raise he

        return json.loads(content)['uuid']
    # end ref_update

    @check_homepage
    def ref_relax_for_delete(self, obj_uuid, ref_uuid):
        # don't account for reference of <obj_uuid> in delete of
        # <ref_uuid> in future
        json_body = json.dumps({'uuid': obj_uuid, 'ref-uuid': ref_uuid})
        uri = self._action_uri['ref-relax-for-delete']

        try:
            content = self._request_server(OP_POST, uri, data=json_body)
        except HttpError as he:
            if he.status_code == 404:
                return None
            raise he

        return json.loads(content)['uuid']
    # end ref_relax_for_delete

    def obj_to_id(self, obj):
        return self.fq_name_to_id(obj.get_type(), obj.get_fq_name())
    # end obj_to_id

    @check_homepage
    def fq_name_to_id(self, obj_type, fq_name):
        json_body = json.dumps({'type': obj_type, 'fq_name': fq_name})
        uri = self._action_uri['name-to-id']
        try:
            content = self._request_server(OP_POST, uri, data=json_body)
        except HttpError as he:
            if he.status_code == 404:
                return None
            raise he

        return json.loads(content)['uuid']
    # end fq_name_to_id

    @check_homepage
    def create_int_pool(self, pool_name, start, end):
        json_body = json.dumps({'pool': pool_name, 'start': start, 'end': end})
        uri = self._action_uri['int-pools']
        self._request_server(OP_POST, uri, data=json_body)
    # end create_int_pool

    @check_homepage
    def delete_int_pool(self, pool_name):
        json_body = json.dumps({'pool': pool_name})
        uri = self._action_uri['int-pools']
        self._request_server(OP_DELETE, uri, data=json_body)
    # end delete_int_pool

    @check_homepage
    def get_int_owner(self, pool_name, index):
        query_params = {'pool': pool_name, 'value': index}
        uri = self._action_uri['int-pool']
        content = self._request_server(OP_GET, uri, data=query_params)
        return content['owner']
    # end get_int_owner

    @check_homepage
    def allocate_int(self, pool_name, owner=""):
        json_body = json.dumps({'pool': pool_name, "owner": owner})
        uri = self._action_uri['int-pool']
        content = self._request_server(OP_POST, uri, data=json_body)
        return json.loads(content)['value']
    # end allocate_int

    @check_homepage
    def set_int(self, pool_name, value, owner=""):
        json_body = json.dumps({'pool': pool_name, 'owner': owner, 'value': value})
        uri = self._action_uri['int-pool']
        self._request_server(OP_POST, uri, data=json_body)
    # end set_int

    @check_homepage
    def deallocate_int(self, pool_name, index):
        json_body = json.dumps({'pool': pool_name, 'value': index})
        uri = self._action_uri['int-pool']
        self._request_server(OP_DELETE, uri, data=json_body)
    # end deallocate_int

    @check_homepage
    def id_to_fq_name(self, id):
        json_body = json.dumps({'uuid': id})
        uri = self._action_uri['id-to-name']
        content = self._request_server(OP_POST, uri, data=json_body)

        return json.loads(content)['fq_name']
    # end id_to_fq_name

    @check_homepage
    def id_to_fq_name_type(self, id):
        json_body = json.dumps({'uuid': id})
        uri = self._action_uri['id-to-name']
        content = self._request_server(OP_POST, uri, data=json_body)

        json_rsp = json.loads(content)
        return (json_rsp['fq_name'], json_rsp['type'])

    # This is required only for helping ifmap-subscribers using rest publish
    @check_homepage
    def ifmap_to_id(self, ifmap_id):
        return None
    # end ifmap_to_id

    def obj_to_json(self, obj):
        return json.dumps(obj, default=_obj_serializer_all)
    # end obj_to_json

    def obj_to_dict(self, obj):
        return json.loads(self.obj_to_json(obj))
    # end obj_to_dict

    @check_homepage
    def fetch_records(self):
        json_body = json.dumps({'fetch_records': None})
        uri = self._action_uri['fetch-records']
        content = self._request_server(OP_POST, uri, data=json_body)

        return json.loads(content)['results']
    # end fetch_records

    @check_homepage
    def restore_config(self, create, resource, json_body):
        cls = obj_type_to_vnc_class(resource, __name__)
        if not cls:
            return None

        if create:
            uri = cls.create_uri
            content = self._request_server(OP_POST, uri, data=json_body)
        else:
            obj_dict = json.loads(json_body)
            uri = cls.resource_uri_base[resource] + '/'
            uri += obj_dict[resource]['uuid']
            content = self._request_server(OP_PUT, uri, data=json_body)

        return json.loads(content)
    # end restore_config

    @check_homepage
    def kv_store(self, key, value):
        # TODO move oper value to common
        json_body = json.dumps({'operation': 'STORE',
                                'key': key,
                                'value': value})
        uri = self._action_uri['useragent-keyvalue']
        self._request_server(OP_POST, uri, data=json_body)
    # end kv_store

    @check_homepage
    def kv_retrieve(self, key=None):
        # if key is None, entire collection is retrieved, use with caution!
        # TODO move oper value to common
        json_body = json.dumps({'operation': 'RETRIEVE',
                                'key': key})
        uri = self._action_uri['useragent-keyvalue']
        content = self._request_server(OP_POST, uri, data=json_body)

        return json.loads(content)['value']
    # end kv_retrieve

    @check_homepage
    def kv_delete(self, key):
        # TODO move oper value to common
        json_body = json.dumps({'operation': 'DELETE',
                                'key': key})
        uri = self._action_uri['useragent-keyvalue']
        self._request_server(OP_POST, uri, data=json_body)
    # end kv_delete

    # reserve block of IP address from a VN
    # expected format {"subnet" : "subnet_uuid", "count" : 4}
    @check_homepage
    def virtual_network_ip_alloc(self, vnobj, count=1,
                                 subnet=None, family=None):
        json_body = json.dumps({'count': count,
                                'subnet': subnet,
                                'family': family})
        uri = self._action_uri['virtual-network-ip-alloc'] % vnobj.uuid
        content = self._request_server(OP_POST, uri, data=json_body)
        return json.loads(content)['ip_addr']
    # end virtual_network_ip_alloc

    # free previously reserved block of IP address from a VN
    # Expected format "ip_addr" : ["2.1.1.239", "2.1.1.238"]
    @check_homepage
    def virtual_network_ip_free(self, vnobj, ip_list):
        json_body = json.dumps({'ip_addr': ip_list})
        uri = self._action_uri['virtual-network-ip-free'] % vnobj.uuid
        rv = self._request_server(OP_POST, uri, data=json_body)
        return rv
    # end virtual_network_ip_free

    # return no of ip instances from a given VN/Subnet
    # Expected format "subne_list" : ["subnet_uuid1", "subnet_uuid2"]
    @check_homepage
    def virtual_network_subnet_ip_count(self, vnobj, subnet_list):
        json_body = json.dumps({'subnet_list': subnet_list})
        uri = self._action_uri['virtual-network-subnet-ip-count'] % vnobj.uuid
        rv = self._request_server(OP_POST, uri, data=json_body)
        return rv
    # end virtual_network_subnet_ip_count

    def get_auth_token(self):
        self._headers = self._authenticate(headers=self._headers)
        return self._auth_token

    # end get_auth_token

    @check_homepage
    def resource_list(self, obj_type, parent_id=None, parent_fq_name=None,
                      back_ref_id=None, obj_uuids=None, fields=None,
                      detail=False, count=False, filters=None, shared=False,
                      token=None, fq_names=None):
        empty_result = [] if detail else {'%ss' % (obj_type): []}
        if obj_uuids == [] or back_ref_id == []:
            return empty_result
        self._headers['X-USER-TOKEN'] = token
        if not obj_type:
            raise ResourceTypeUnknownError(obj_type)

        obj_class = obj_type_to_vnc_class(obj_type, __name__)
        if not obj_class:
            raise ResourceTypeUnknownError(obj_type)

        query_params = {}
        do_post_for_list = False

        if parent_fq_name:
            parent_fq_name_str = ':'.join(parent_fq_name)
            query_params['parent_fq_name_str'] = parent_fq_name_str
        elif parent_id:
            if isinstance(parent_id, list):
                query_params['parent_id'] = ','.join(parent_id)
                if len(parent_id) > self.POST_FOR_LIST_THRESHOLD:
                    do_post_for_list = True
            else:
                query_params['parent_id'] = parent_id

        if back_ref_id:
            if isinstance(back_ref_id, list):
                query_params['back_ref_id'] = ','.join(back_ref_id)
                if len(back_ref_id) > self.POST_FOR_LIST_THRESHOLD:
                    do_post_for_list = True
            else:
                query_params['back_ref_id'] = back_ref_id

        if obj_uuids:
            comma_sep_obj_uuids = ','.join(u for u in obj_uuids)
            query_params['obj_uuids'] = comma_sep_obj_uuids
            if len(obj_uuids) > self.POST_FOR_LIST_THRESHOLD:
                do_post_for_list = True

        if fq_names:
            comma_sep_fq_names = ','.join([':'.join(u) for u in fq_names])
            query_params['fq_names'] = comma_sep_fq_names
            if len(fq_names) > self.POST_FOR_LIST_THRESHOLD:
                do_post_for_list = True

        fields = set(fields or [])
        if fields:
            # filter fields with only known attributes
            if detail:
                # when details is true, VNC API returns at least all properties
                # and refs fields, don't need to specify them
                fields = (
                    fields & (obj_class.backref_fields | obj_class.children_fields)
                )
            else:
                fields = (fields & (
                    obj_class.prop_fields |
                    obj_class.children_fields |
                    obj_class.ref_fields |
                    obj_class.backref_fields)
                )

            query_params['fields'] = ','.join(f for f in fields)

        query_params['detail'] = detail

        query_params['count'] = count
        query_params['shared'] = shared

        if filters:
            query_params['filters'] = ''
            for key, value in filters.items():
                if isinstance(value, list):
                    query_params['filters'] += ','.join(
                        '%s==%s' % (key, json.dumps(val)) for val in value)
                else:
                    query_params['filters'] += ('%s==%s' %
                                                (key, json.dumps(value)))
                query_params['filters'] += ','
            # Remove last trailing comma
            query_params['filters'] = query_params['filters'][:-1]

        if self._exclude_hrefs is not None:
            query_params['exclude_hrefs'] = True

        if do_post_for_list:
            uri = self._action_uri.get('list-bulk-collection')
            if not uri:
                raise

            # use same keys as in GET with additional 'type'
            query_params['type'] = obj_type
            json_body = json.dumps(query_params)
            content = self._request_server(OP_POST,
                                           uri, json_body)
            response = json.loads(content)
        else:  # GET /<collection>
            try:
                response = self._request_server(
                    OP_GET, obj_class.create_uri, data=query_params)
            except NoIdError:
                # dont allow NoIdError propagate to user
                return empty_result

        if not detail:
            return response

        resource_dicts = response['%ss' % (obj_type)]
        resource_objs = []
        for resource_dict in resource_dicts:
            obj_dict = resource_dict['%s' % (obj_type)]
            # if requested child/backref fields are not in the result, that
            # means resource does not have child/backref of that type. Set it
            # to None to prevent VNC client lib to call again VNC API when user
            # uses the get child/backref method on that type in the
            # 'resource_client' file
            [obj_dict.setdefault(field, None) for field in fields]
            resource_obj = obj_class.from_dict(**obj_dict)
            resource_obj.clear_pending_updates()
            resource_obj.set_server_conn(self)
            resource_objs.append(resource_obj)

        if 'X-USER-TOKEN' in self._headers:
            del self._headers['X-USER-TOKEN']
        return resource_objs
    # end resource_list

    def set_auth_token(self, token):
        """Park user token for forwarding to API server for RBAC."""
        self._headers['X-AUTH-TOKEN'] = token
        self._auth_token_input = True
    # end set_auth_token

    def set_user_roles(self, roles):
        """Park user roles for forwarding to API server for RBAC.

        :param roles: list of roles
        """
        self._headers['X-API-ROLE'] = (',').join(roles)
    # end set_user_roles

    def set_exclude_hrefs(self):
        self._exclude_hrefs = True
    # end set_exclude_hrefs

    @check_homepage
    def obj_perms(self, token, obj_uuid=None):
        """
        validate user token. Optionally, check token authorization
        for an object.
        rv {'token_info': <token-info>, 'permissions': 'RWX'}
        """
        if token is not None:
            self._headers['X-USER-TOKEN'] = token
        query = 'uuid=%s' % obj_uuid if obj_uuid else ''
        try:
            rv = self._request_server(OP_GET, "/obj-perms", data=query)
            return rv
        except PermissionDenied:
            rv = None
        finally:
            if 'X-USER-TOKEN' in self._headers:
                del self._headers['X-USER-TOKEN']
        return rv

    @check_homepage
    def amqp_publish(self, exchange=None, exchange_type='direct',
                     routing_key=None, headers=None, payload=''):
        if exchange is None:
            raise ValueError("Exchange must be specified")

        body = {
            'exchange': exchange,
            'exchange_type': exchange_type,
            'routing_key': routing_key,
            'payload': payload
        }
        if headers:
            body['headers'] = headers

        uri = self._action_uri['amqp-publish']
        json_body = json.dumps(body)
        self._request_server(OP_POST, uri, data=json_body)
    # end amqp_publish

    @check_homepage
    def amqp_request(self, exchange=None, exchange_type='direct',
                     routing_key=None, response_key=None,
                     headers=None, payload=''):
        if exchange is None or response_key is None:
            raise ValueError("Exchange and response key must be specified")

        body = {
            'exchange': exchange,
            'exchange_type': exchange_type,
            'routing_key': routing_key,
            'response_key': response_key,
            'payload': payload
        }
        if headers:
            body['headers'] = headers

        uri = self._action_uri['amqp-request']
        json_body = json.dumps(body)
        content = self._request_server(OP_POST, uri, data=json_body)
        return json.loads(content)
    # end amqp_request

    def is_cloud_admin_role(self):
        rv = self.obj_perms(self.get_auth_token()) or {}
        return rv.get('is_cloud_admin_role', False)

    def is_global_read_only_role(self):
        rv = self.obj_perms(self.get_auth_token()) or {}
        return rv.get('is_global_read_only_role', False)

    # change object ownsership
    def chown(self, obj_uuid, owner):
        payload = {'uuid': obj_uuid, 'owner': owner}
        content = self._request_server(
            OP_POST, self._action_uri['chown'],
            data=json.dumps(payload))
        return content
    # end chown

    def chmod(self, obj_uuid, owner=None, owner_access=None,
              share=None, global_access=None):
        """
        owner: tenant UUID
        owner_access: octal permission for owner (int, 0-7)
        share: list of tuple of <uuid:octal-perms>,
               for example [(0ed5ea...700:7)]
        global_access: octal permission for global access (int, 0-7)
        """
        payload = {'uuid': obj_uuid}
        if owner:
            payload['owner'] = owner
        if owner_access is not None:
            payload['owner_access'] = owner_access
        if share is not None:
            payload['share'] = [{'tenant': item[0], 'tenant_access': item[1]}
                                for item in share]
        if global_access is not None:
            payload['global_access'] = global_access
        content = self._request_server(
            OP_POST, self._action_uri['chmod'],
            data=json.dumps(payload))
        return content

    def set_aaa_mode(self, mode):
        if mode not in AAA_MODE_VALID_VALUES:
            raise HttpError(400, 'Invalid AAA mode')
        url = self._action_uri['aaa-mode']
        data = {'aaa-mode': mode}
        content = self._request_server(OP_PUT, url, json.dumps(data))
        return json.loads(content)

    def get_aaa_mode(self):
        url = self._action_uri['aaa-mode']
        rv = self._request_server(OP_GET, url)
        return rv

    def set_tags(self, obj, tags_dict):
        """Associate or disassociate one or multiple tags to a resource

        Adds or remove tags to a resource and also permits to set/unset
        multiple values for tags which are authorized to be set multiple time
        on a same resource (ie. `label` tag only).

        :param obj: Resource object to update associated tags
        :param tags_dict: Dict indexed by tag type name that describe values to
            add or remove. If the corresponding value of a Tag type is None,
            all referenced to that Tag type will be removed. For example:
            {
                'application': {
                    'is_global': True,
                    'value': 'production',
                },
                'label': {
                    'is_global': False,
                    'add_values': ['blue', 'grey'],
                    'delete_values': ['red'],
                },
                'tier': {
                    'value': 'backend',
                },
                'foo': None,
            }
        """
        url = self._action_uri['set-tag']
        data = {
            'obj_type': obj.object_type,
            'obj_uuid': obj.get_uuid(),
        }
        data.update(tags_dict)
        content = self._request_server(OP_POST, url, json.dumps(data))
        return json.loads(content)

    def set_tag(self, obj, type, value, is_global=False):
        """Associate a defined tag to a resource

        :param obj: Resource object to associate the defined tag
        :param type: Tag type name
        :param value: Tag value
        :param is_global: Set tag global to all project (default: False)
        """
        tags_dict = {
            type: {
                'is_global': is_global,
                'value': value,
            },
        }
        return self.set_tags(obj, tags_dict)

    def unset_tag(self, obj, type):
        """Disassociate tags of a certain type

        :param obj: Resource object to disassociate tags
        :param type: tag type name
        """
        tags_dict = {
            type: None,
        }
        return self.set_tags(obj, tags_dict)

    def _security_policy_draft(self, action, scope):
        """Commit or discard pending resources on a given scope

        :param action: specify action to be done: commit or discard
        :param scope: Scope that own the pending security resource (aka. Global
            global policy management or project)
        """
        if action not in ['commit', 'discard']:
            msg = "Only 'commit' or 'discard' actions are supported"
            raise ValueError(msg)

        url = self._action_uri['security-policy-draft']
        data = {
            'scope_uuid': scope.uuid,
            'action': action,
        }
        content = self._request_server(OP_POST, url, json.dumps(data))
        return json.loads(content)

    def commit_security(self, scope):
        """Commit pending resources on a given scope

        :param scope: Scope that own the pending security resource to commit
            (aka. Global global policy management or project)
        """
        self._security_policy_draft('commit', scope)

    def discard_security(self, scope):
        """discard pending resources on a given scope

        :param scope: Scope that own the pending security resource to discard
            (aka. Global global policy management or project)
        """
        self._security_policy_draft('discard', scope)
# end class VncApi
