# vim: tabstop=4 shiftwidth=4 softtabstop=4
# Copyright 2012 Nebula, Inc.
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

import base64
import copy
import datetime
import inspect
import json
import os
import re
import urllib
import uuid as uuid_lib

import coverage
from lxml import etree
from oslo.config import cfg

from nova.api.metadata import password
from nova.api.openstack.compute.contrib import coverage_ext
from nova.api.openstack.compute.contrib import fping
# Import extensions to pull in osapi_compute_extension CONF option used below.
from nova.cloudpipe import pipelib
from nova.compute import api as compute_api
from nova.compute import manager as compute_manager
from nova import context
from nova import db
from nova.db.sqlalchemy import models
from nova import exception
from nova.network import api as network_api
from nova.openstack.common import importutils
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova.openstack.common import timeutils
import nova.quota
from nova.scheduler import driver
from nova.servicegroup import api as service_group_api
from nova import test
from nova.tests.api.openstack.compute.contrib import test_coverage_ext
from nova.tests.api.openstack.compute.contrib import test_fping
from nova.tests.api.openstack.compute.contrib import test_networks
from nova.tests.api.openstack.compute.contrib import test_services
from nova.tests.api.openstack import fakes
from nova.tests.baremetal.db import base as bm_db_base
from nova.tests import fake_instance_actions
from nova.tests import fake_network
from nova.tests.image import fake
from nova.tests.integrated import integrated_helpers
from nova.tests import utils as test_utils
from nova import utils
from nova.volume import cinder

CONF = cfg.CONF
CONF.import_opt('allow_resize_to_same_host', 'nova.compute.api')
CONF.import_opt('osapi_compute_extension',
                'nova.api.openstack.compute.extensions')
CONF.import_opt('vpn_image_id', 'nova.cloudpipe.pipelib')
CONF.import_opt('osapi_compute_link_prefix', 'nova.api.openstack.common')
CONF.import_opt('osapi_glance_link_prefix', 'nova.api.openstack.common')
CONF.import_opt('enable', 'nova.cells.opts', group='cells')
CONF.import_opt('db_check_interval', 'nova.cells.state', group='cells')
LOG = logging.getLogger(__name__)


class NoMatch(test.TestingException):
    pass


class ApiSampleTestBase(integrated_helpers._IntegratedTestBase):
    ctype = 'json'
    all_extensions = False
    extension_name = None

    def setUp(self):
        self.flags(use_ipv6=False,
                   osapi_compute_link_prefix=self._get_host(),
                   osapi_glance_link_prefix=self._get_glance_host())
        if not self.all_extensions:
            ext = [self.extension_name] if self.extension_name else []
            self.flags(osapi_compute_extension=ext)
        super(ApiSampleTestBase, self).setUp()
        fake_network.stub_compute_with_ips(self.stubs)
        self.generate_samples = os.getenv('GENERATE_SAMPLES') is not None

    def _pretty_data(self, data):
        if self.ctype == 'json':
            data = jsonutils.dumps(jsonutils.loads(data), sort_keys=True,
                    indent=4)

        else:
            if data is None:
                # Likely from missing XML file.
                return ""
            xml = etree.XML(data)
            data = etree.tostring(xml, encoding="UTF-8",
                    xml_declaration=True, pretty_print=True)
        return '\n'.join(line.rstrip() for line in data.split('\n')).strip()

    def _objectify(self, data):
        if not data:
            return {}
        if self.ctype == 'json':
            # NOTE(vish): allow non-quoted replacements to survive json
            data = re.sub(r'([^"])%\((.+)\)s([^"])', r'\1"%(int:\2)s"\3', data)
            return jsonutils.loads(data)
        else:
            def to_dict(node):
                ret = {}
                if node.items():
                    ret.update(dict(node.items()))
                if node.text:
                    ret['__content__'] = node.text
                if node.tag:
                    ret['__tag__'] = node.tag
                if node.nsmap:
                    ret['__nsmap__'] = node.nsmap
                for element in node:
                    ret.setdefault(node.tag, [])
                    ret[node.tag].append(to_dict(element))
                return ret
            return to_dict(etree.fromstring(data))

    @classmethod
    def _get_sample_path(cls, name, dirname, suffix=''):
        parts = [dirname]
        parts.append('api_samples')
        if cls.all_extensions:
            parts.append('all_extensions')
        if cls.extension_name:
            alias = importutils.import_class(cls.extension_name).alias
            parts.append(alias)
        parts.append(name + "." + cls.ctype + suffix)
        return os.path.join(*parts)

    @classmethod
    def _get_sample(cls, name):
        dirname = os.path.dirname(os.path.abspath(__file__))
        dirname = os.path.join(dirname, "../../../doc")
        return cls._get_sample_path(name, dirname)

    @classmethod
    def _get_template(cls, name):
        dirname = os.path.dirname(os.path.abspath(__file__))
        return cls._get_sample_path(name, dirname, suffix='.tpl')

    def _read_template(self, name):
        template = self._get_template(name)
        with open(template) as inf:
            return inf.read().strip()

    def _write_template(self, name, data):
        with open(self._get_template(name), 'w') as outf:
            outf.write(data)

    def _write_sample(self, name, data):
        with open(self._get_sample(name), 'w') as outf:
            outf.write(data)

    def _compare_result(self, subs, expected, result, result_str):
        matched_value = None
        if isinstance(expected, dict):
            if not isinstance(result, dict):
                raise NoMatch(_('%(result_str)s: %(result)s is not a dict.')
                              % locals())
            ex_keys = sorted(expected.keys())
            res_keys = sorted(result.keys())
            if ex_keys != res_keys:
                ex_delta = []
                res_delta = []
                for key in ex_keys:
                    if key not in res_keys:
                        ex_delta.append(key)
                for key in res_keys:
                    if key not in ex_keys:
                        res_delta.append(key)
                raise NoMatch(
                        _('Dictionary key mismatch:\n'
                        'Extra key(s) in template:\n%(ex_delta)s\n'
                        'Extra key(s) in %(result_str)s:\n%(res_delta)s\n')
                        % locals())
            for key in ex_keys:
                res = self._compare_result(subs, expected[key], result[key],
                                           result_str)
                matched_value = res or matched_value
        elif isinstance(expected, list):
            if not isinstance(result, list):
                raise NoMatch(
                     _('%(result_str)s: %(result)s is not a list.') % locals())

            expected = expected[:]
            extra = []
            for res_obj in result:
                for i, ex_obj in enumerate(expected):
                    try:
                        matched_value = self._compare_result(subs, ex_obj,
                                                             res_obj,
                                                             result_str)
                        del expected[i]
                        break
                    except NoMatch:
                        pass
                else:
                    extra.append(res_obj)

            error = []
            if expected:
                error.append(_('Extra list items in template:'))
                error.extend([repr(o) for o in expected])

            if extra:
                error.append(_('Extra list items in %(result_str)s:')
                             % locals())
                error.extend([repr(o) for o in extra])

            if error:
                raise NoMatch('\n'.join(error))
        elif isinstance(expected, basestring) and '%' in expected:
            # NOTE(vish): escape stuff for regex
            for char in '[]<>?':
                expected = expected.replace(char, '\\%s' % char)
            # NOTE(vish): special handling of subs that are not quoted. We are
            #             expecting an int but we had to pass in a string
            #             so the json would parse properly.
            if expected.startswith("%(int:"):
                result = str(result)
                expected = expected.replace('int:', '')
            expected = expected % subs
            expected = '^%s$' % expected
            match = re.match(expected, result)
            if not match:
                raise NoMatch(
                    _('Values do not match:\n'
                    'Template: %(expected)s\n%(result_str)s: %(result)s')
                    % locals())
            try:
                matched_value = match.group('id')
            except IndexError:
                if match.groups():
                    matched_value = match.groups()[0]
        else:
            if isinstance(expected, basestring):
                # NOTE(danms): Ignore whitespace in this comparison
                expected = expected.strip()
                result = result.strip()
            if expected != result:
                raise NoMatch(
                        _('Values do not match:\n'
                        'Template: %(expected)s\n%(result_str)s: %(result)s')
                        % locals())
        return matched_value

    def generalize_subs(self, subs, vanilla_regexes):
        """Give the test a chance to modify subs after the server response
        was verified, and before the on-disk doc/api_samples file is checked.
        This may be needed by some tests to convert exact matches expected
        from the server into pattern matches to verify what is in the
        sample file.

        If there are no changes to be made, subs is returned unharmed.
        """
        return subs

    def _verify_response(self, name, subs, response):
        response_data = response.read()
        response_data = self._pretty_data(response_data)
        if not os.path.exists(self._get_template(name)):
            self._write_template(name, response_data)
            template_data = response_data
        else:
            template_data = self._read_template(name)

        if (self.generate_samples and
            not os.path.exists(self._get_sample(name))):
            self._write_sample(name, response_data)
            sample_data = response_data
        else:
            with file(self._get_sample(name)) as sample:
                sample_data = sample.read()

        try:
            template_data = self._objectify(template_data)
            response_data = self._objectify(response_data)
            response_result = self._compare_result(subs, template_data,
                                                   response_data, "Response")
            # NOTE(danms): replace some of the subs with patterns for the
            # doc/api_samples check, which won't have things like the
            # correct compute host name. Also let the test do some of its
            # own generalization, if necessary
            vanilla_regexes = self._get_regexes()
            subs['compute_host'] = vanilla_regexes['host_name']
            subs['id'] = vanilla_regexes['id']
            subs = self.generalize_subs(subs, vanilla_regexes)
            sample_data = self._objectify(sample_data)
            self._compare_result(subs, template_data, sample_data, "Sample")
            return response_result
        except NoMatch:
            raise

    def _get_host(self):
        return 'http://openstack.example.com'

    def _get_glance_host(self):
        return 'http://glance.openstack.example.com'

    def _get_regexes(self):
        if self.ctype == 'json':
            text = r'(\\"|[^"])*'
        else:
            text = r'[^<]*'
        return {
            # NOTE(treinish): Could result in a false positive, but it
            # shouldn't be an issue for this case.
            'timestamp': '\d{4}-[0,1]\d-[0-3]\d[ ,T]'
                         '\d{2}:\d{2}:\d{2}'
                         '(Z|(\+|-)\d{2}:\d{2}|\.\d{6}|)',
            'password': '[0-9a-zA-Z]{1,12}',
            'ip': '[0-9]{1,3}.[0-9]{1,3}.[0-9]{1,3}.[0-9]{1,3}',
            'ip6': '([0-9a-zA-Z]{1,4}:){1,7}:?[0-9a-zA-Z]{1,4}',
            'id': '(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}'
                  '-[0-9a-f]{4}-[0-9a-f]{12})',
            'uuid': '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}'
                    '-[0-9a-f]{4}-[0-9a-f]{12}',
            'reservation_id': 'r-[0-9a-zA-Z]{8}',
            'private_key': '-----BEGIN RSA PRIVATE KEY-----'
                           '[a-zA-Z0-9\n/+=]*'
                           '-----END RSA PRIVATE KEY-----',
            'public_key': 'ssh-rsa[ a-zA-Z0-9/+=]*'
                          'Generated by Nova',
            'fingerprint': '([0-9a-f]{2}:){15}[0-9a-f]{2}',
#            '[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:'
#                           '[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:'
#                           '[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:'
#                           '[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}',
            'host': self._get_host(),
            'host_name': '[0-9a-z]{32}',
            'glance_host': self._get_glance_host(),
            'compute_host': self.compute.host,
            'text': text,
            'int': '[0-9]+',
        }

    def _get_response(self, url, method, body=None, strip_version=False):
        headers = {}
        headers['Content-Type'] = 'application/' + self.ctype
        headers['Accept'] = 'application/' + self.ctype
        return self.api.api_request(url, body=body, method=method,
                headers=headers, strip_version=strip_version)

    def _do_get(self, url, strip_version=False):
        return self._get_response(url, 'GET', strip_version=strip_version)

    def _do_post(self, url, name, subs, method='POST'):
        body = self._read_template(name) % subs
        sample = self._get_sample(name)
        if self.generate_samples and not os.path.exists(sample):
                self._write_sample(name, body)
        return self._get_response(url, method, body)

    def _do_put(self, url, name, subs):
        return self._do_post(url, name, subs, method='PUT')

    def _do_delete(self, url):
        return self._get_response(url, 'DELETE')


class ApiSamplesTrap(ApiSampleTestBase):
    """Make sure extensions don't get added without tests."""

    all_extensions = True

    def _get_extensions_tested(self):
        tests = []
        for attr in globals().values():
            if not inspect.isclass(attr):
                continue  # Skip non-class objects
            if not issubclass(attr, integrated_helpers._IntegratedTestBase):
                continue  # Skip non-test classes
            if attr.extension_name is None:
                continue  # Skip base tests
            cls = importutils.import_class(attr.extension_name)
            tests.append(cls.alias)
        return tests

    def _get_extensions(self):
        extensions = []
        response = self._do_get('extensions')
        for extension in jsonutils.loads(response.read())['extensions']:
            extensions.append(str(extension['alias']))
        return extensions

    def test_all_extensions_have_samples(self):
        # NOTE(danms): This is a list of extensions which are currently
        # in the tree but that don't (yet) have tests. This list should
        # NOT be allowed to grow, and should shrink to zero (and be
        # removed) soon.
        do_not_approve_additions = []
        do_not_approve_additions.append('os-create-server-ext')

        tests = self._get_extensions_tested()
        extensions = self._get_extensions()
        missing_tests = []
        for extension in extensions:
            # NOTE(danms): if you add tests, remove it from the
            # exclusions list
            self.assertFalse(extension in do_not_approve_additions and
                             extension in tests)

            # NOTE(danms): if you add an extension, it must come with
            # api_samples tests!
            if (extension not in tests and
                extension not in do_not_approve_additions):
                missing_tests.append(extension)

        if missing_tests:
            LOG.error("Extensions are missing tests: %s" % missing_tests)
        self.assertEqual(missing_tests, [])


class VersionsSampleJsonTest(ApiSampleTestBase):
    def test_versions_get(self):
        response = self._do_get('', strip_version=True)
        subs = self._get_regexes()
        return self._verify_response('versions-get-resp', subs, response)


class VersionsSampleXmlTest(VersionsSampleJsonTest):
    ctype = 'xml'


class ServersSampleBase(ApiSampleTestBase):
    def _post_server(self):
        subs = {
            'image_id': fake.get_valid_image_id(),
            'host': self._get_host(),
        }
        response = self._do_post('servers', 'server-post-req', subs)
        self.assertEqual(response.status, 202)
        subs = self._get_regexes()
        return self._verify_response('server-post-resp', subs, response)


class ServersSampleJsonTest(ServersSampleBase):
    def test_servers_post(self):
        return self._post_server()

    def test_servers_get(self):
        uuid = self.test_servers_post()
        response = self._do_get('servers/%s' % uuid)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        subs['id'] = uuid
        subs['hypervisor_hostname'] = r'[\w\.\-]+'
        return self._verify_response('server-get-resp', subs, response)

    def test_servers_list(self):
        uuid = self._post_server()
        response = self._do_get('servers')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['id'] = uuid
        return self._verify_response('servers-list-resp', subs, response)

    def test_servers_details(self):
        uuid = self._post_server()
        response = self._do_get('servers/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        subs['id'] = uuid
        subs['hypervisor_hostname'] = r'[\w\.\-]+'
        return self._verify_response('servers-details-resp', subs, response)


class ServersSampleXmlTest(ServersSampleJsonTest):
    ctype = 'xml'


class ServersSampleAllExtensionJsonTest(ServersSampleJsonTest):
    all_extensions = True


class ServersSampleAllExtensionXmlTest(ServersSampleXmlTest):
    all_extensions = True


class ServersSampleHideAddressesJsonTest(ServersSampleJsonTest):
    extension_name = '.'.join(('nova.api.openstack.compute.contrib',
                               'hide_server_addresses',
                               'Hide_server_addresses'))


class ServersSampleHideAddressesXMLTest(ServersSampleHideAddressesJsonTest):
    ctype = 'xml'


class ServersMetadataJsonTest(ServersSampleBase):
    def _create_and_set(self, subs):
        uuid = self._post_server()
        response = self._do_put('servers/%s/metadata' % uuid,
                                'server-metadata-all-req',
                                subs)
        self.assertEqual(response.status, 200)
        self._verify_response('server-metadata-all-resp', subs, response)

        return uuid

    def generalize_subs(self, subs, vanilla_regexes):
        subs['value'] = '(Foo|Bar) Value'
        return subs

    def test_metadata_put_all(self):
        # Test setting all metadata for a server.
        subs = {'value': 'Foo Value'}
        return self._create_and_set(subs)

    def test_metadata_post_all(self):
        # Test updating all metadata for a server.
        subs = {'value': 'Foo Value'}
        uuid = self._create_and_set(subs)
        subs['value'] = 'Bar Value'
        response = self._do_post('servers/%s/metadata' % uuid,
                                 'server-metadata-all-req',
                                 subs)
        self.assertEqual(response.status, 200)
        self._verify_response('server-metadata-all-resp', subs, response)

    def test_metadata_get_all(self):
        # Test getting all metadata for a server.
        subs = {'value': 'Foo Value'}
        uuid = self._create_and_set(subs)
        response = self._do_get('servers/%s/metadata' % uuid)
        self.assertEqual(response.status, 200)
        self._verify_response('server-metadata-all-resp', subs, response)

    def test_metadata_put(self):
        # Test putting an individual metadata item for a server.
        subs = {'value': 'Foo Value'}
        uuid = self._create_and_set(subs)
        subs['value'] = 'Bar Value'
        response = self._do_put('servers/%s/metadata/foo' % uuid,
                                'server-metadata-req',
                                subs)
        self.assertEqual(response.status, 200)
        return self._verify_response('server-metadata-resp', subs, response)

    def test_metadata_get(self):
        # Test getting an individual metadata item for a server.
        subs = {'value': 'Foo Value'}
        uuid = self._create_and_set(subs)
        response = self._do_get('servers/%s/metadata/foo' % uuid)
        self.assertEqual(response.status, 200)
        return self._verify_response('server-metadata-resp', subs, response)

    def test_metadata_delete(self):
        # Test deleting an individual metadata item for a server.
        subs = {'value': 'Foo Value'}
        uuid = self._create_and_set(subs)
        response = self._do_delete('servers/%s/metadata/foo' % uuid)
        self.assertEqual(response.status, 204)
        self.assertEqual(response.read(), '')


class ServersMetadataXmlTest(ServersMetadataJsonTest):
    ctype = 'xml'


class ServersIpsJsonTest(ServersSampleBase):
    def test_get(self):
        # Test getting a server's IP information.
        uuid = self._post_server()
        response = self._do_get('servers/%s/ips' % uuid)
        subs = self._get_regexes()
        return self._verify_response('server-ips-resp', subs, response)

    def test_get_by_network(self):
        # Test getting a server's IP information by network id.
        uuid = self._post_server()
        response = self._do_get('servers/%s/ips/private' % uuid)
        subs = self._get_regexes()
        return self._verify_response('server-ips-network-resp', subs, response)


class ServersIpsXmlTest(ServersIpsJsonTest):
    ctype = 'xml'


class ExtensionsSampleJsonTest(ApiSampleTestBase):
    all_extensions = True

    def test_extensions_get(self):
        response = self._do_get('extensions')
        subs = self._get_regexes()
        return self._verify_response('extensions-get-resp', subs, response)


class ExtensionsSampleXmlTest(ExtensionsSampleJsonTest):
    ctype = 'xml'


class FlavorsSampleJsonTest(ApiSampleTestBase):

    def test_flavors_get(self):
        response = self._do_get('flavors/1')
        subs = self._get_regexes()
        return self._verify_response('flavor-get-resp', subs, response)

    def test_flavors_list(self):
        response = self._do_get('flavors')
        subs = self._get_regexes()
        return self._verify_response('flavors-list-resp', subs, response)


class FlavorsSampleXmlTest(FlavorsSampleJsonTest):
    ctype = 'xml'


class HostsSampleJsonTest(ApiSampleTestBase):
    extension_name = "nova.api.openstack.compute.contrib.hosts.Hosts"

    def test_host_startup(self):
        response = self._do_get('os-hosts/%s/startup' % self.compute.host)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('host-get-startup', subs, response)

    def test_host_reboot(self):
        response = self._do_get('os-hosts/%s/reboot' % self.compute.host)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('host-get-reboot', subs, response)

    def test_host_shutdown(self):
        response = self._do_get('os-hosts/%s/shutdown' % self.compute.host)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('host-get-shutdown', subs, response)

    def test_host_maintenance(self):
        response = self._do_put('os-hosts/%s' % self.compute.host,
                                'host-put-maintenance-req', {})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('host-put-maintenance-resp', subs,
                                     response)

    def test_host_get(self):
        response = self._do_get('os-hosts/%s' % self.compute.host)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('host-get-resp', subs, response)

    def test_hosts_list(self):
        response = self._do_get('os-hosts')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('hosts-list-resp', subs, response)


class HostsSampleXmlTest(HostsSampleJsonTest):
    ctype = 'xml'


class FlavorsSampleAllExtensionJsonTest(FlavorsSampleJsonTest):
    all_extensions = True


class FlavorsSampleAllExtensionXmlTest(FlavorsSampleXmlTest):
    all_extensions = True


class ImagesSampleJsonTest(ApiSampleTestBase):
    def test_images_list(self):
        # Get api sample of images get list request.
        response = self._do_get('images')
        subs = self._get_regexes()
        return self._verify_response('images-list-get-resp', subs, response)

    def test_image_get(self):
        # Get api sample of one single image details request.
        image_id = fake.get_valid_image_id()
        response = self._do_get('images/%s' % image_id)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['image_id'] = image_id
        return self._verify_response('image-get-resp', subs, response)

    def test_images_details(self):
        # Get api sample of all images details request.
        response = self._do_get('images/detail')
        subs = self._get_regexes()
        return self._verify_response('images-details-get-resp', subs, response)

    def test_image_metadata_get(self):
        # Get api sample of an image metadata request.
        image_id = fake.get_valid_image_id()
        response = self._do_get('images/%s/metadata' % image_id)
        subs = self._get_regexes()
        subs['image_id'] = image_id
        return self._verify_response('image-metadata-get-resp', subs, response)

    def test_image_metadata_post(self):
        # Get api sample to update metadata of an image metadata request.
        image_id = fake.get_valid_image_id()
        response = self._do_post(
                'images/%s/metadata' % image_id,
                'image-metadata-post-req', {})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('image-metadata-post-resp',
                                     subs, response)

    def test_image_metadata_put(self):
        # Get api sample of image metadata put request.
        image_id = fake.get_valid_image_id()
        response = self._do_put('images/%s/metadata' % image_id,
                                'image-metadata-put-req', {})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('image-metadata-put-resp',
                                     subs, response)

    def test_image_meta_key_get(self):
        # Get api sample of an image metadata key request.
        image_id = fake.get_valid_image_id()
        key = "kernel_id"
        response = self._do_get('images/%s/metadata/%s' % (image_id, key))
        subs = self._get_regexes()
        return self._verify_response('image-meta-key-get', subs, response)

    def test_image_meta_key_put(self):
        # Get api sample of image metadata key put request.
        image_id = fake.get_valid_image_id()
        key = "auto_disk_config"
        response = self._do_put('images/%s/metadata/%s' % (image_id, key),
                                'image-meta-key-put-req', {})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('image-meta-key-put-resp',
                                     subs,
                                     response)


class ImagesSampleXmlTest(ImagesSampleJsonTest):
    ctype = 'xml'


class LimitsSampleJsonTest(ApiSampleTestBase):
    def test_limits_get(self):
        response = self._do_get('limits')
        subs = self._get_regexes()
        return self._verify_response('limit-get-resp', subs, response)


class LimitsSampleXmlTest(LimitsSampleJsonTest):
    ctype = 'xml'


class CoverageExtJsonTests(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.coverage_ext."
                      "Coverage_ext")

    def setUp(self):
        super(CoverageExtJsonTests, self).setUp()

        def _fake_check_coverage(self):
            return False

        def _fake_xml_report(self, outfile=None):
            return

        self.stubs.Set(coverage_ext.CoverageController, '_check_coverage',
                       _fake_check_coverage)
        self.stubs.Set(coverage, 'coverage', test_coverage_ext.FakeCoverage)

    def test_start_coverage(self):
        # Start coverage data collection.
        subs = {}
        response = self._do_post('os-coverage/action',
                                 'coverage-start-post-req', subs)
        self.assertEqual(response.status, 200)

    def test_start_coverage_combine(self):
        # Start coverage data collection.
        subs = {}
        response = self._do_post('os-coverage/action',
                                 'coverage-start-combine-post-req', subs)
        self.assertEqual(response.status, 200)

    def test_stop_coverage(self):
        # Stop coverage data collection.
        subs = {
            'path': '/.*',
        }
        response = self._do_post('os-coverage/action',
                                 'coverage-stop-post-req', subs)
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response('coverage-stop-post-resp',
                                     subs, response)

    def test_report_coverage(self):
        # Generate a coverage report.
        subs = {
            'filename': 'report',
            'path': '/.*/report',
        }
        response = self._do_post('os-coverage/action',
                                 'coverage-report-post-req', subs)
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response('coverage-report-post-resp',
                                     subs, response)

    def test_xml_report_coverage(self):
        subs = {
            'filename': 'report',
            'path': '/.*/report',
        }
        response = self._do_post('os-coverage/action',
                                 'coverage-xml-report-post-req', subs)
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response('coverage-xml-report-post-resp',
                                     subs, response)


class CoverageExtXmlTests(CoverageExtJsonTests):
    ctype = "xml"


class ServersActionsJsonTest(ServersSampleBase):
    def _test_server_action(self, uuid, action,
                            subs={}, resp_tpl=None, code=202):
        subs.update({'action': action})
        response = self._do_post('servers/%s/action' % uuid,
                                 'server-action-%s' % action.lower(),
                                 subs)
        self.assertEqual(response.status, code)
        if resp_tpl:
            subs.update(self._get_regexes())
            return self._verify_response(resp_tpl, subs, response)
        else:
            self.assertEqual(response.read(), "")

    def test_server_password(self):
        uuid = self._post_server()
        self._test_server_action(uuid, "changePassword",
                                 {"password": "foo"})

    def test_server_reboot(self):
        uuid = self._post_server()
        self._test_server_action(uuid, "reboot",
                                 {"type": "HARD"})
        self._test_server_action(uuid, "reboot",
                                 {"type": "SOFT"})

    def test_server_rebuild(self):
        uuid = self._post_server()
        image = self.api.get_images()[0]['id']
        subs = {'host': self._get_host(),
                'uuid': image,
                'name': 'foobar',
                'pass': 'seekr3t',
                'ip': '1.2.3.4',
                'ip6': 'fe80::100',
                'hostid': '[a-f0-9]+',
                }
        self._test_server_action(uuid, 'rebuild', subs,
                                 'server-action-rebuild-resp')

    def test_server_resize(self):
        self.flags(allow_resize_to_same_host=True)
        uuid = self._post_server()
        self._test_server_action(uuid, "resize",
                                 {"id": 2,
                                  "host": self._get_host()})
        return uuid

    def test_server_revert_resize(self):
        uuid = self.test_server_resize()
        self._test_server_action(uuid, "revertResize")

    def test_server_confirm_resize(self):
        uuid = self.test_server_resize()
        self._test_server_action(uuid, "confirmResize", code=204)

    def test_server_create_image(self):
        uuid = self._post_server()
        self._test_server_action(uuid, 'createImage',
                                 {'name': 'foo-image',
                                  'meta_var': 'myvar',
                                  'meta_val': 'foobar'})


class ServersActionsXmlTest(ServersActionsJsonTest):
    ctype = 'xml'


class ServersActionsAllJsonTest(ServersActionsJsonTest):
    all_extensions = True


class ServersActionsAllXmlTest(ServersActionsXmlTest):
    all_extensions = True


class ServerStartStopJsonTest(ServersSampleBase):
    extension_name = "nova.api.openstack.compute.contrib" + \
        ".server_start_stop.Server_start_stop"

    def _test_server_action(self, uuid, action):
        response = self._do_post('servers/%s/action' % uuid,
                                 'server_start_stop',
                                 {'action': action})
        self.assertEqual(response.status, 202)
        self.assertEqual(response.read(), "")

    def test_server_start(self):
        uuid = self._post_server()
        self._test_server_action(uuid, 'os-stop')
        self._test_server_action(uuid, 'os-start')

    def test_server_stop(self):
        uuid = self._post_server()
        self._test_server_action(uuid, 'os-stop')


class ServerStartStopXmlTest(ServerStartStopJsonTest):
    ctype = 'xml'


class UserDataJsonTest(ApiSampleTestBase):
    extension_name = "nova.api.openstack.compute.contrib.user_data.User_data"

    def test_user_data_post(self):
        user_data_contents = '#!/bin/bash\n/bin/su\necho "I am in you!"\n'
        user_data = base64.b64encode(user_data_contents)
        subs = {
            'image_id': fake.get_valid_image_id(),
            'host': self._get_host(),
            'user_data': user_data
            }
        response = self._do_post('servers', 'userdata-post-req', subs)

        self.assertEqual(response.status, 202)
        subs.update(self._get_regexes())
        return self._verify_response('userdata-post-resp', subs, response)


class UserDataXmlTest(UserDataJsonTest):
    ctype = 'xml'


class FlavorsExtraDataJsonTest(ApiSampleTestBase):
    extension_name = ('nova.api.openstack.compute.contrib.flavorextradata.'
                      'Flavorextradata')

    def _get_flags(self):
        f = super(FlavorsExtraDataJsonTest, self)._get_flags()
        f['osapi_compute_extension'] = CONF.osapi_compute_extension[:]
        # Flavorextradata extension also needs Flavormanage to be loaded.
        f['osapi_compute_extension'].append(
            'nova.api.openstack.compute.contrib.flavormanage.Flavormanage')
        return f

    def test_flavors_extra_data_get(self):
        flavor_id = 1
        response = self._do_get('flavors/%s' % flavor_id)
        self.assertEqual(response.status, 200)
        subs = {
            'flavor_id': flavor_id,
            'flavor_name': 'm1.tiny'
        }
        subs.update(self._get_regexes())
        return self._verify_response('flavors-extra-data-get-resp', subs,
                                     response)

    def test_flavors_extra_data_list(self):
        response = self._do_get('flavors/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('flavors-extra-data-list-resp', subs,
                                     response)

    def test_flavors_extra_data_create(self):
        subs = {
            'flavor_id': 666,
            'flavor_name': 'flavortest'
        }
        response = self._do_post('flavors',
                                 'flavors-extra-data-post-req',
                                 subs)
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response('flavors-extra-data-post-resp',
                                     subs, response)


class FlavorsExtraDataXmlTest(FlavorsExtraDataJsonTest):
    ctype = 'xml'


class FlavorRxtxJsonTest(ApiSampleTestBase):
    extension_name = ('nova.api.openstack.compute.contrib.flavor_rxtx.'
                      'Flavor_rxtx')

    def _get_flags(self):
        f = super(FlavorRxtxJsonTest, self)._get_flags()
        f['osapi_compute_extension'] = CONF.osapi_compute_extension[:]
        # FlavorRxtx extension also needs Flavormanage to be loaded.
        f['osapi_compute_extension'].append(
            'nova.api.openstack.compute.contrib.flavormanage.Flavormanage')
        return f

    def test_flavor_rxtx_get(self):
        flavor_id = 1
        response = self._do_get('flavors/%s' % flavor_id)
        self.assertEqual(response.status, 200)
        subs = {
            'flavor_id': flavor_id,
            'flavor_name': 'm1.tiny'
        }
        subs.update(self._get_regexes())
        return self._verify_response('flavor-rxtx-get-resp', subs,
                                     response)

    def test_flavors_rxtx_list(self):
        response = self._do_get('flavors/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('flavor-rxtx-list-resp', subs,
                                     response)

    def test_flavors_rxtx_create(self):
        subs = {
            'flavor_id': 100,
            'flavor_name': 'flavortest'
        }
        response = self._do_post('flavors',
                                 'flavor-rxtx-post-req',
                                 subs)
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response('flavor-rxtx-post-resp',
                                     subs, response)


class FlavorRxtxXmlTest(FlavorRxtxJsonTest):
    ctype = 'xml'


class FlavorSwapJsonTest(ApiSampleTestBase):
    extension_name = ('nova.api.openstack.compute.contrib.flavor_swap.'
                      'Flavor_swap')

    def _get_flags(self):
        f = super(FlavorSwapJsonTest, self)._get_flags()
        f['osapi_compute_extension'] = CONF.osapi_compute_extension[:]
        # FlavorSwap extension also needs Flavormanage to be loaded.
        f['osapi_compute_extension'].append(
            'nova.api.openstack.compute.contrib.flavormanage.Flavormanage')
        return f

    def test_flavor_swap_get(self):
        flavor_id = 1
        response = self._do_get('flavors/%s' % flavor_id)
        self.assertEqual(response.status, 200)
        subs = {
            'flavor_id': flavor_id,
            'flavor_name': 'm1.tiny'
        }
        subs.update(self._get_regexes())
        return self._verify_response('flavor-swap-get-resp', subs,
                                     response)

    def test_flavor_swap_list(self):
        response = self._do_get('flavors/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('flavor-swap-list-resp', subs,
                                     response)

    def test_flavor_swap_create(self):
        subs = {
            'flavor_id': 100,
            'flavor_name': 'flavortest'
        }
        response = self._do_post('flavors',
                                 'flavor-swap-post-req',
                                 subs)
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response('flavor-swap-post-resp',
                                     subs, response)


class FlavorSwapXmlTest(FlavorSwapJsonTest):
    ctype = 'xml'


class SecurityGroupsSampleJsonTest(ServersSampleBase):
    extension_name = "nova.api.openstack.compute.contrib" + \
                     ".security_groups.Security_groups"

    def test_security_group_create(self):
        name = self.ctype + '-test'
        subs = {
                'group_name': name,
                "description": "description",
        }
        response = self._do_post('os-security-groups',
                                 'security-group-post-req', subs)
        self.assertEqual(response.status, 200)
        self._verify_response('security-groups-create-resp', subs, response)

    def test_security_groups_list(self):
        # Get api sample of security groups get list request.
        response = self._do_get('os-security-groups')
        subs = self._get_regexes()
        return self._verify_response('security-groups-list-get-resp',
                                      subs, response)

    def test_security_groups_get(self):
        # Get api sample of security groups get request.
        security_group_id = '1'
        response = self._do_get('os-security-groups/%s' % security_group_id)
        subs = self._get_regexes()
        return self._verify_response('security-groups-get-resp',
                                      subs, response)

    def test_security_groups_list_server(self):
        # Get api sample of security groups for a specific server.
        uuid = self._post_server()
        response = self._do_get('servers/%s/os-security-groups' % uuid)
        subs = self._get_regexes()
        return self._verify_response('server-security-groups-list-resp',
                                      subs, response)


class SecurityGroupsSampleXmlTest(ApiSampleTestBase):
    ctype = 'xml'


class SecurityGroupDefaultRulesSampleJsonTest(ServersSampleBase):
    extension_name = ('nova.api.openstack.compute.contrib'
                      '.security_group_default_rules'
                      '.Security_group_default_rules')

    def test_security_group_default_rules_create(self):
        response = self._do_post('os-security-group-default-rules',
                                 'security-group-default-rules-create-req',
                                 {})
        self.assertEqual(response.status, 200)
        return self._verify_response(
            'security-group-default-rules-create-resp', {}, response)

    def test_security_group_default_rules_list(self):
        self.test_security_group_default_rules_create()
        response = self._do_get('os-security-group-default-rules')
        return self._verify_response('security-group-default-rules-list-resp',
                                     {}, response)

    def test_security_group_default_rules_show(self):
        self.test_security_group_default_rules_create()
        rule_id = '1'
        response = self._do_get('os-security-group-default-rules/%s' % rule_id)
        return self._verify_response('security-group-default-rules-show-resp',
                                     {}, response)


class SecurityGroupDefaultRulesSampleXmlTest(
                                    SecurityGroupDefaultRulesSampleJsonTest):
    ctype = 'xml'


class SchedulerHintsJsonTest(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.scheduler_hints."
                     "Scheduler_hints")

    def test_scheduler_hints_post(self):
        # Get api sample of scheduler hint post request.
        hints = {'image_id': fake.get_valid_image_id(),
                 'image_near': str(uuid_lib.uuid4())
        }
        response = self._do_post('servers', 'scheduler-hints-post-req',
                                 hints)
        self.assertEqual(response.status, 202)
        subs = self._get_regexes()
        return self._verify_response('scheduler-hints-post-resp', subs,
                                     response)


class SchedulerHintsXmlTest(SchedulerHintsJsonTest):
    ctype = 'xml'


class ConsoleOutputSampleJsonTest(ServersSampleBase):
    extension_name = "nova.api.openstack.compute.contrib" + \
                                     ".console_output.Console_output"

    def test_get_console_output(self):
        uuid = self._post_server()
        response = self._do_post('servers/%s/action' % uuid,
                                 'console-output-post-req',
                                {'action': 'os-getConsoleOutput'})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('console-output-post-resp',
                                       subs, response)


class ConsoleOutputSampleXmlTest(ConsoleOutputSampleJsonTest):
        ctype = 'xml'


class ExtendedServerAttributesJsonTest(ServersSampleBase):
    extension_name = "nova.api.openstack.compute.contrib" + \
                     ".extended_server_attributes" + \
                     ".Extended_server_attributes"

    def test_show(self):
        uuid = self._post_server()

        response = self._do_get('servers/%s' % uuid)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        subs['id'] = uuid
        subs['instance_name'] = 'instance-\d{8}'
        subs['hypervisor_hostname'] = r'[\w\.\-]+'
        return self._verify_response('server-get-resp',
                                     subs, response)

    def test_detail(self):
        uuid = self._post_server()

        response = self._do_get('servers/detail')
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        subs['id'] = uuid
        subs['instance_name'] = 'instance-\d{8}'
        subs['hypervisor_hostname'] = r'[\w\.\-]+'
        return self._verify_response('servers-detail-resp',
                                     subs, response)


class ExtendedServerAttributesXmlTest(ExtendedServerAttributesJsonTest):
    ctype = 'xml'


class FloatingIpsJsonTest(ApiSampleTestBase):
    extension_name = "nova.api.openstack.compute.contrib." \
        "floating_ips.Floating_ips"

    def setUp(self):
        super(FloatingIpsJsonTest, self).setUp()
        pool = CONF.default_floating_pool
        interface = CONF.public_interface

        self.ip_pool = [
            {
                'address': "10.10.10.1",
                'pool': pool,
                'interface': interface
                },
            {
                'address': "10.10.10.2",
                'pool': pool,
                'interface': interface
                },
            {
                'address': "10.10.10.3",
                'pool': pool,
                'interface': interface
                },
            ]
        self.compute.db.floating_ip_bulk_create(
            context.get_admin_context(), self.ip_pool)

    def tearDown(self):
        self.compute.db.floating_ip_bulk_destroy(
            context.get_admin_context(), self.ip_pool)
        super(FloatingIpsJsonTest, self).tearDown()

    def test_floating_ips_list_empty(self):
        response = self._do_get('os-floating-ips')

        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('floating-ips-list-empty-resp',
                                     subs, response)

    def test_floating_ips_list(self):
        self._do_post('os-floating-ips',
                      'floating-ips-create-nopool-req',
                      {})
        self._do_post('os-floating-ips',
                      'floating-ips-create-nopool-req',
                      {})

        response = self._do_get('os-floating-ips')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('floating-ips-list-resp',
                                     subs, response)

    def test_floating_ips_create_nopool(self):
        response = self._do_post('os-floating-ips',
                                 'floating-ips-create-nopool-req',
                                 {})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        self._verify_response('floating-ips-create-resp',
                              subs, response)

    def test_floating_ips_create(self):
        response = self._do_post('os-floating-ips',
                                 'floating-ips-create-req',
                                 {"pool": CONF.default_floating_pool})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        self._verify_response('floating-ips-create-resp',
                              subs, response)

    def test_floating_ips_get(self):
        self.test_floating_ips_create()
        # NOTE(sdague): the first floating ip will always have 1 as an id,
        # but it would be better if we could get this from the create
        response = self._do_get('os-floating-ips/%d' % 1)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        self._verify_response('floating-ips-create-resp',
                              subs, response)

    def test_floating_ips_delete(self):
        self.test_floating_ips_create()
        response = self._do_delete('os-floating-ips/%d' % 1)
        self.assertEqual(response.status, 202)


class FloatingIpsXmlTest(FloatingIpsJsonTest):
    ctype = 'xml'


class FloatingIpsBulkJsonTest(ApiSampleTestBase):
    extension_name = "nova.api.openstack.compute.contrib." \
        "floating_ips_bulk.Floating_ips_bulk"

    def setUp(self):
        super(FloatingIpsBulkJsonTest, self).setUp()
        pool = CONF.default_floating_pool
        interface = CONF.public_interface

        self.ip_pool = [
            {
                'address': "10.10.10.1",
                'pool': pool,
                'interface': interface
                },
            {
                'address': "10.10.10.2",
                'pool': pool,
                'interface': interface
                },
            {
                'address': "10.10.10.3",
                'pool': pool,
                'interface': interface,
                'host': "testHost"
                },
            ]
        self.compute.db.floating_ip_bulk_create(
            context.get_admin_context(), self.ip_pool)

    def tearDown(self):
        self.compute.db.floating_ip_bulk_destroy(
            context.get_admin_context(), self.ip_pool)
        super(FloatingIpsBulkJsonTest, self).tearDown()

    def test_floating_ips_bulk_list(self):
        response = self._do_get('os-floating-ips-bulk')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('floating-ips-bulk-list-resp', subs,
                                     response)

    def test_floating_ips_bulk_list_by_host(self):
        response = self._do_get('os-floating-ips-bulk/testHost')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('floating-ips-bulk-list-by-host-resp',
                                     subs, response)

    def test_floating_ips_bulk_create(self):
        response = self._do_post('os-floating-ips-bulk',
                                 'floating-ips-bulk-create-req',
                                 {"ip_range": "192.168.1.0/24",
                                  "pool": CONF.default_floating_pool,
                                  "interface": CONF.public_interface})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('floating-ips-bulk-create-resp', subs,
                                     response)

    def test_floating_ips_bulk_delete(self):
        response = self._do_put('os-floating-ips-bulk/delete',
                                'floating-ips-bulk-delete-req',
                                {"ip_range": "192.168.1.0/24"})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('floating-ips-bulk-delete-resp', subs,
                                     response)


class FloatingIpsBulkXmlTest(FloatingIpsBulkJsonTest):
    ctype = 'xml'


class KeyPairsSampleJsonTest(ApiSampleTestBase):
    extension_name = "nova.api.openstack.compute.contrib.keypairs.Keypairs"

    def generalize_subs(self, subs, vanilla_regexes):
        subs['keypair_name'] = 'keypair-[0-9a-f-]+'
        return subs

    def test_keypairs_post(self, public_key=None):
        """Get api sample of key pairs post request."""
        key_name = 'keypair-' + str(uuid_lib.uuid4())
        response = self._do_post('os-keypairs', 'keypairs-post-req',
                                 {'keypair_name': key_name})
        subs = self._get_regexes()
        subs['keypair_name'] = '(%s)' % key_name
        self.assertEqual(response.status, 200)
        self._verify_response('keypairs-post-resp', subs, response)
        # NOTE(maurosr): return the key_name is necessary cause the
        # verification returns the label of the last compared information in
        # the response, not necessarily the key name.
        return key_name

    def test_keypairs_import_key_post(self):
        # Get api sample of key pairs post to import user's key.
        key_name = 'keypair-' + str(uuid_lib.uuid4())
        subs = {
            'keypair_name': key_name,
            'public_key': "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAAgQDx8nkQv/zgGg"
                          "B4rMYmIf+6A4l6Rr+o/6lHBQdW5aYd44bd8JttDCE/F/pNRr0l"
                          "RE+PiqSPO8nDPHw0010JeMH9gYgnnFlyY3/OcJ02RhIPyyxYpv"
                          "9FhY+2YiUkpwFOcLImyrxEsYXpD/0d3ac30bNH6Sw9JD9UZHYc"
                          "pSxsIbECHw== Generated by Nova"
        }
        response = self._do_post('os-keypairs', 'keypairs-import-post-req',
                                 subs)
        subs = self._get_regexes()
        subs['keypair_name'] = '(%s)' % key_name
        self.assertEqual(response.status, 200)
        self._verify_response('keypairs-import-post-resp', subs, response)

    def test_keypairs_get(self):
        # Get api sample of key pairs get request.
        key_name = self.test_keypairs_post()
        response = self._do_get('os-keypairs')
        subs = self._get_regexes()
        subs['keypair_name'] = '(%s)' % key_name
        return self._verify_response('keypairs-get-resp', subs, response)


class KeyPairsSampleXmlTest(KeyPairsSampleJsonTest):
    ctype = 'xml'


class RescueJsonTest(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib"
                     ".rescue.Rescue")

    def _rescue(self, uuid):
        req_subs = {
            'password': 'MySecretPass'
        }
        response = self._do_post('servers/%s/action' % uuid,
                                 'server-rescue-req', req_subs)
        self._verify_response('server-rescue', req_subs, response)

    def _unrescue(self, uuid):
        response = self._do_post('servers/%s/action' % uuid,
                                 'server-unrescue-req', {})
        self.assertEqual(response.status, 202)

    def test_server_rescue(self):
        uuid = self._post_server()

        self._rescue(uuid)

        # Do a server get to make sure that the 'RESCUE' state is set
        response = self._do_get('servers/%s' % uuid)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        subs['id'] = uuid
        subs['status'] = 'RESCUE'

        self._verify_response('server-get-resp-rescue', subs, response)

    def test_server_unrescue(self):
        uuid = self._post_server()

        self._rescue(uuid)
        self._unrescue(uuid)

        # Do a server get to make sure that the 'ACTIVE' state is back
        response = self._do_get('servers/%s' % uuid)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        subs['id'] = uuid
        subs['status'] = 'ACTIVE'

        self._verify_response('server-get-resp-unrescue', subs, response)


class RescueXmlTest(RescueJsonTest):
    ctype = 'xml'


class VirtualInterfacesJsonTest(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib"
                     ".virtual_interfaces.Virtual_interfaces")

    def test_vifs_list(self):
        uuid = self._post_server()

        response = self._do_get('servers/%s/os-virtual-interfaces' % uuid)
        self.assertEqual(response.status, 200)

        subs = self._get_regexes()
        subs['mac_addr'] = '(?:[a-f0-9]{2}:){5}[a-f0-9]{2}'

        self._verify_response('vifs-list-resp', subs, response)


class VirtualInterfacesXmlTest(VirtualInterfacesJsonTest):
    ctype = 'xml'


class CloudPipeSampleJsonTest(ApiSampleTestBase):
    extension_name = "nova.api.openstack.compute.contrib.cloudpipe.Cloudpipe"

    def setUp(self):
        super(CloudPipeSampleJsonTest, self).setUp()

        def get_user_data(self, project_id):
            """Stub method to generate user data for cloudpipe tests."""
            return "VVNFUiBEQVRB\n"

        def network_api_get(self, context, network_uuid):
            """Stub to get a valid network and its information."""
            return {'vpn_public_address': '127.0.0.1',
                    'vpn_public_port': 22}

        self.stubs.Set(pipelib.CloudPipe, 'get_encoded_zip', get_user_data)
        self.stubs.Set(network_api.API, "get",
                       network_api_get)

    def generalize_subs(self, subs, vanilla_regexes):
        subs['project_id'] = 'cloudpipe-[0-9a-f-]+'
        return subs

    def test_cloud_pipe_create(self):
        # Get api samples of cloud pipe extension creation.
        self.flags(vpn_image_id=fake.get_valid_image_id())
        project = {'project_id': 'cloudpipe-' + str(uuid_lib.uuid4())}
        response = self._do_post('os-cloudpipe', 'cloud-pipe-create-req',
                                 project)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs.update(project)
        subs['image_id'] = CONF.vpn_image_id
        self._verify_response('cloud-pipe-create-resp', subs, response)
        return project

    def test_cloud_pipe_list(self):
        # Get api samples of cloud pipe extension get request.
        project = self.test_cloud_pipe_create()
        response = self._do_get('os-cloudpipe')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs.update(project)
        subs['image_id'] = CONF.vpn_image_id
        return self._verify_response('cloud-pipe-get-resp', subs, response)


class CloudPipeSampleXmlTest(CloudPipeSampleJsonTest):
    ctype = "xml"


class CloudPipeUpdateJsonTest(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib"
                      ".cloudpipe_update.Cloudpipe_update")

    def _get_flags(self):
        f = super(CloudPipeUpdateJsonTest, self)._get_flags()
        f['osapi_compute_extension'] = CONF.osapi_compute_extension[:]
        # Cloudpipe_update also needs cloudpipe to be loaded
        f['osapi_compute_extension'].append(
            'nova.api.openstack.compute.contrib.cloudpipe.Cloudpipe')
        return f

    def test_cloud_pipe_update(self):
        subs = {'vpn_ip': '192.168.1.1',
                'vpn_port': 2000}
        response = self._do_put('os-cloudpipe/configure-project',
                                'cloud-pipe-update-req',
                                subs)
        self.assertEqual(response.status, 202)


class CloudPipeUpdateXmlTest(CloudPipeUpdateJsonTest):
    ctype = "xml"


class AgentsJsonTest(ApiSampleTestBase):
    extension_name = "nova.api.openstack.compute.contrib.agents.Agents"

    def _get_flags(self):
        f = super(AgentsJsonTest, self)._get_flags()
        f['osapi_compute_extension'] = CONF.osapi_compute_extension[:]
        return f

    def setUp(self):
        super(AgentsJsonTest, self).setUp()

        fake_agents_list = [{'url': 'xxxxxxxxxxxx',
                             'hypervisor': 'hypervisor',
                             'architecture': 'x86',
                             'os': 'os',
                             'version': '8.0',
                             'md5hash': 'add6bb58e139be103324d04d82d8f545',
                             'id': '1'}]

        def fake_agent_build_create(context, values):
            values['id'] = '1'
            agent_build_ref = models.AgentBuild()
            agent_build_ref.update(values)
            return agent_build_ref

        def fake_agent_build_get_all(context, hypervisor):
            agent_build_all = []
            for agent in fake_agents_list:
                if hypervisor and hypervisor != agent['hypervisor']:
                    continue
                agent_build_ref = models.AgentBuild()
                agent_build_ref.update(agent)
                agent_build_all.append(agent_build_ref)
            return agent_build_all

        def fake_agent_build_update(context, agent_build_id, values):
            pass

        def fake_agent_build_destroy(context, agent_update_id):
            pass

        self.stubs.Set(db, "agent_build_create",
                       fake_agent_build_create)
        self.stubs.Set(db, "agent_build_get_all",
                       fake_agent_build_get_all)
        self.stubs.Set(db, "agent_build_update",
                       fake_agent_build_update)
        self.stubs.Set(db, "agent_build_destroy",
                       fake_agent_build_destroy)

    def test_agent_create(self):
        # Creates a new agent build.
        project = {'url': 'xxxxxxxxxxxx',
                'hypervisor': 'hypervisor',
                'architecture': 'x86',
                'os': 'os',
                'version': '8.0',
                'md5hash': 'add6bb58e139be103324d04d82d8f545'
                }
        response = self._do_post('os-agents', 'agent-post-req',
                                 project)
        self.assertEqual(response.status, 200)
        project['agent_id'] = 1
        self._verify_response('agent-post-resp', project, response)
        return project

    def test_agent_list(self):
        # Return a list of all agent builds.
        response = self._do_get('os-agents')
        self.assertEqual(response.status, 200)
        project = {'url': 'xxxxxxxxxxxx',
                'hypervisor': 'hypervisor',
                'architecture': 'x86',
                'os': 'os',
                'version': '8.0',
                'md5hash': 'add6bb58e139be103324d04d82d8f545',
                'agent_id': 1
                }
        return self._verify_response('agents-get-resp', project, response)

    def test_agent_update(self):
        # Update an existing agent build.
        agent_id = 1
        subs = {'version': '7.0',
                'url': 'xxx://xxxx/xxx/xxx',
                'md5hash': 'add6bb58e139be103324d04d82d8f545'}
        response = self._do_put('os-agents/%s' % agent_id,
                                'agent-update-put-req', subs)
        self.assertEqual(response.status, 200)
        subs['agent_id'] = 1
        return self._verify_response('agent-update-put-resp', subs, response)

    def test_agent_delete(self):
        # Deletes an existing agent build.
        agent_id = 1
        response = self._do_delete('os-agents/%s' % agent_id)
        self.assertEqual(response.status, 200)


class AgentsXmlTest(AgentsJsonTest):
    ctype = "xml"


class FixedIpJsonTest(ApiSampleTestBase):
    extension_name = "nova.api.openstack.compute.contrib.fixed_ips.Fixed_ips"

    def _get_flags(self):
        f = super(FixedIpJsonTest, self)._get_flags()
        f['osapi_compute_extension'] = CONF.osapi_compute_extension[:]
        return f

    def setUp(self):
        super(FixedIpJsonTest, self).setUp()

        fake_fixed_ips = [{'id': 1,
                   'address': '192.168.1.1',
                   'network_id': 1,
                   'virtual_interface_id': 1,
                   'instance_uuid': '1',
                   'allocated': False,
                   'leased': False,
                   'reserved': False,
                   'host': None},
                  {'id': 2,
                   'address': '192.168.1.2',
                   'network_id': 1,
                   'virtual_interface_id': 2,
                   'instance_uuid': '2',
                   'allocated': False,
                   'leased': False,
                   'reserved': False,
                   'host': None},
                  ]

        def fake_fixed_ip_get_by_address(context, address):
            for fixed_ip in fake_fixed_ips:
                if fixed_ip['address'] == address:
                    return fixed_ip
            raise exception.FixedIpNotFoundForAddress(address=address)

        def fake_fixed_ip_get_by_address_detailed(context, address):
            network = {'id': 1,
                       'cidr': "192.168.1.0/24"}
            host = {'host': "host",
                    'hostname': 'openstack'}
            for fixed_ip in fake_fixed_ips:
                if fixed_ip['address'] == address:
                    return (fixed_ip, network, host)
            raise exception.FixedIpNotFoundForAddress(address=address)

        def fake_fixed_ip_update(context, address, values):
            fixed_ip = fake_fixed_ip_get_by_address(context, address)
            if fixed_ip is None:
                raise exception.FixedIpNotFoundForAddress(address=address)
            else:
                for key in values:
                    fixed_ip[key] = values[key]

        self.stubs.Set(db, "fixed_ip_get_by_address",
                       fake_fixed_ip_get_by_address)
        self.stubs.Set(db, "fixed_ip_get_by_address_detailed",
                       fake_fixed_ip_get_by_address_detailed)
        self.stubs.Set(db, "fixed_ip_update", fake_fixed_ip_update)

    def test_fixed_ip_reserve(self):
        # Reserve a Fixed IP.
        project = {'reserve': None}
        response = self._do_post('os-fixed-ips/192.168.1.1/action',
                                 'fixedip-post-req',
                                 project)
        self.assertEqual(response.status, 202)

    def test_get_fixed_ip(self):
        # Return data about the given fixed ip.
        response = self._do_get('os-fixed-ips/192.168.1.1')
        self.assertEqual(response.status, 200)
        project = {'cidr': '192.168.1.0/24',
                   'hostname': 'openstack',
                   'host': 'host',
                   'address': '192.168.1.1'}
        return self._verify_response('fixedips-get-resp', project, response)


class FixedIpXmlTest(FixedIpJsonTest):
    ctype = "xml"


class AggregatesSampleJsonTest(ServersSampleBase):
    extension_name = "nova.api.openstack.compute.contrib" + \
                                     ".aggregates.Aggregates"

    def test_aggregate_create(self):
        subs = {
            "aggregate_id": '(?P<id>\d+)'
        }
        response = self._do_post('os-aggregates', 'aggregate-post-req', subs)
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response('aggregate-post-resp', subs, response)

    def test_list_aggregates(self):
        self.test_aggregate_create()
        response = self._do_get('os-aggregates')
        subs = self._get_regexes()
        return self._verify_response('aggregates-list-get-resp',
                                      subs, response)

    def test_aggregate_get(self):
        agg_id = self.test_aggregate_create()
        response = self._do_get('os-aggregates/%s' % agg_id)
        subs = self._get_regexes()
        return self._verify_response('aggregates-get-resp', subs, response)

    def test_add_metadata(self):
        agg_id = self.test_aggregate_create()
        response = self._do_post('os-aggregates/%s/action' % agg_id,
                                 'aggregate-metadata-post-req',
                                 {'action': 'set_metadata'})
        subs = self._get_regexes()
        return self._verify_response('aggregates-metadata-post-resp',
                                      subs, response)

    def test_add_host(self):
        aggregate_id = self.test_aggregate_create()
        subs = {
            "host_name": self.compute.host,
        }
        response = self._do_post('os-aggregates/%s/action' % aggregate_id,
                                 'aggregate-add-host-post-req', subs)
        subs.update(self._get_regexes())
        return self._verify_response('aggregates-add-host-post-resp',
                                      subs, response)

    def test_remove_host(self):
        self.test_add_host()
        subs = {
            "host_name": self.compute.host,
        }
        response = self._do_post('os-aggregates/1/action',
                                 'aggregate-remove-host-post-req', subs)
        subs.update(self._get_regexes())
        return self._verify_response('aggregates-remove-host-post-resp',
                                      subs, response)

    def test_update_aggregate(self):
        aggregate_id = self.test_aggregate_create()
        response = self._do_put('os-aggregates/%s' % aggregate_id,
                                  'aggregate-update-post-req', {})
        subs = self._get_regexes()
        return self._verify_response('aggregate-update-post-resp',
                                      subs, response)


class AggregatesSampleXmlTest(AggregatesSampleJsonTest):
    ctype = 'xml'


class CertificatesSamplesJsonTest(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.certificates."
                      "Certificates")

    def test_create_certificates(self):
        response = self._do_post('os-certificates',
                                 'certificate-create-req', {})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('certificate-create-resp', subs, response)

    def test_get_root_certificate(self):
        response = self._do_get('os-certificates/root')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('certificate-get-root-resp', subs,
                                     response)


class CertificatesSamplesXmlTest(CertificatesSamplesJsonTest):
    ctype = 'xml'


class UsedLimitsSamplesJsonTest(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.used_limits."
                      "Used_limits")

    def test_get_used_limits(self):
        # Get api sample to used limits.
        response = self._do_get('limits')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('usedlimits-get-resp', subs, response)


class UsedLimitsSamplesXmlTest(UsedLimitsSamplesJsonTest):
    ctype = "xml"


class MultipleCreateJsonTest(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib.multiple_create."
                      "Multiple_create")

    def test_multiple_create(self):
        subs = {
            'image_id': fake.get_valid_image_id(),
            'host': self._get_host(),
            'min_count': "2",
            'max_count': "3"
        }
        response = self._do_post('servers', 'multiple-create-post-req', subs)
        self.assertEqual(response.status, 202)
        subs.update(self._get_regexes())
        return self._verify_response('multiple-create-post-resp',
                                      subs, response)

    def test_multiple_create_without_reservation_id(self):
        subs = {
            'image_id': fake.get_valid_image_id(),
            'host': self._get_host(),
            'min_count': "2",
            'max_count': "3"
        }
        response = self._do_post('servers', 'multiple-create-no-resv-post-req',
                                  subs)
        self.assertEqual(response.status, 202)
        subs.update(self._get_regexes())
        return self._verify_response('multiple-create-no-resv-post-resp',
                                      subs, response)


class MultipleCreateXmlTest(MultipleCreateJsonTest):
    ctype = 'xml'


class ServicesJsonTest(ApiSampleTestBase):
    extension_name = "nova.api.openstack.compute.contrib.services.Services"

    def setUp(self):
        super(ServicesJsonTest, self).setUp()
        self.stubs.Set(db, "service_get_all",
                       test_services.fake_db_api_service_get_all)
        self.stubs.Set(timeutils, "utcnow", test_services.fake_utcnow)
        self.stubs.Set(db, "service_get_by_args",
                       test_services.fake_service_get_by_host_binary)
        self.stubs.Set(db, "service_update",
                       test_services.fake_service_update)

    def tearDown(self):
        super(ServicesJsonTest, self).tearDown()
        timeutils.clear_time_override()

    def test_services_list(self):
        """Return a list of all agent builds."""
        response = self._do_get('os-services')
        self.assertEqual(response.status, 200)
        subs = {'binary': 'nova-compute',
                'host': 'host1',
                'zone': 'nova',
                'status': 'disabled',
                'state': 'up'}
        subs.update(self._get_regexes())
        return self._verify_response('services-list-get-resp',
                                     subs, response)

    def test_service_enable(self):
        """Enable an existing agent build."""
        subs = {"host": "host1",
                'binary': 'nova-compute'}
        response = self._do_put('os-services/enable',
                                'service-enable-put-req', subs)
        self.assertEqual(response.status, 200)
        subs = {"host": "host1",
                "binary": "nova-compute"}
        return self._verify_response('service-enable-put-resp',
                                      subs, response)

    def test_service_disable(self):
        """Disable an existing agent build."""
        subs = {"host": "host1",
                'binary': 'nova-compute'}
        response = self._do_put('os-services/disable',
                                'service-disable-put-req', subs)
        self.assertEqual(response.status, 200)
        subs = {"host": "host1",
                "binary": "nova-compute"}
        return self._verify_response('service-disable-put-resp',
                                     subs, response)


class ServicesXmlTest(ServicesJsonTest):
    ctype = 'xml'


class SimpleTenantUsageSampleJsonTest(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib.simple_tenant_usage."
                      "Simple_tenant_usage")

    def setUp(self):
        """setUp method for simple tenant usage."""
        super(SimpleTenantUsageSampleJsonTest, self).setUp()
        self._post_server()
        timeutils.set_time_override(timeutils.utcnow() +
                                    datetime.timedelta(hours=1))
        self.query = {
            'start': str(timeutils.utcnow() - datetime.timedelta(hours=1)),
            'end': str(timeutils.utcnow())
        }

    def tearDown(self):
        """tearDown method for simple tenant usage."""
        super(SimpleTenantUsageSampleJsonTest, self).tearDown()
        timeutils.clear_time_override()

    def test_get_tenants_usage(self):
        # Get api sample to get all tenants usage request.
        response = self._do_get('os-simple-tenant-usage?%s' % (
                                                urllib.urlencode(self.query)))
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        self._verify_response('simple-tenant-usage-get', subs, response)

    def test_get_tenant_usage_details(self):
        # Get api sample to get specific tenant usage request.
        tenant_id = 'openstack'
        response = self._do_get('os-simple-tenant-usage/%s?%s' % (tenant_id,
                                                urllib.urlencode(self.query)))
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        self._verify_response('simple-tenant-usage-get-specific', subs,
                              response)


class SimpleTenantUsageSampleXmlTest(SimpleTenantUsageSampleJsonTest):
    ctype = "xml"


class ServerDiagnosticsSamplesJsonTest(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib.server_diagnostics."
                      "Server_diagnostics")

    def test_server_diagnostics_get(self):
        uuid = self._post_server()
        response = self._do_get('servers/%s/diagnostics' % uuid)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('server-diagnostics-get-resp', subs,
                                     response)


class ServerDiagnosticsSamplesXmlTest(ServerDiagnosticsSamplesJsonTest):
    ctype = "xml"


class AvailabilityZoneJsonTest(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib.availability_zone."
                      "Availability_zone")

    def test_create_availability_zone(self):
        subs = {
            'image_id': fake.get_valid_image_id(),
            'host': self._get_host(),
            "availability_zone": "nova"
        }
        response = self._do_post('servers', 'availability-zone-post-req', subs)
        self.assertEqual(response.status, 202)
        subs.update(self._get_regexes())
        return self._verify_response('availability-zone-post-resp',
                                      subs, response)


class AvailabilityZoneXmlTest(AvailabilityZoneJsonTest):
    ctype = "xml"


class AdminActionsSamplesJsonTest(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib.admin_actions."
                      "Admin_actions")

    def setUp(self):
        """setUp Method for AdminActions api samples extension
        This method creates the server that will be used in each tests"""
        super(AdminActionsSamplesJsonTest, self).setUp()
        self.uuid = self._post_server()

    def test_post_pause(self):
        # Get api samples to pause server request.
        response = self._do_post('servers/%s/action' % self.uuid,
                                 'admin-actions-pause', {})
        self.assertEqual(response.status, 202)

    def test_post_unpause(self):
        # Get api samples to unpause server request.
        self.test_post_pause()
        response = self._do_post('servers/%s/action' % self.uuid,
                                 'admin-actions-unpause', {})
        self.assertEqual(response.status, 202)

    def test_post_suspend(self):
        # Get api samples to suspend server request.
        response = self._do_post('servers/%s/action' % self.uuid,
                                 'admin-actions-suspend', {})
        self.assertEqual(response.status, 202)

    def test_post_resume(self):
        # Get api samples to server resume request.
        self.test_post_suspend()
        response = self._do_post('servers/%s/action' % self.uuid,
                                 'admin-actions-resume', {})
        self.assertEqual(response.status, 202)

    def test_post_migrate(self):
        # Get api samples to migrate server request.
        response = self._do_post('servers/%s/action' % self.uuid,
                                 'admin-actions-migrate', {})
        self.assertEqual(response.status, 202)

    def test_post_reset_network(self):
        # Get api samples to reset server network request.
        response = self._do_post('servers/%s/action' % self.uuid,
                                 'admin-actions-reset-network', {})
        self.assertEqual(response.status, 202)

    def test_post_inject_network_info(self):
        # Get api samples to inject network info request.
        response = self._do_post('servers/%s/action' % self.uuid,
                                 'admin-actions-inject-network-info', {})
        self.assertEqual(response.status, 202)

    def test_post_lock_server(self):
        # Get api samples to lock server request.
        response = self._do_post('servers/%s/action' % self.uuid,
                                 'admin-actions-lock-server', {})
        self.assertEqual(response.status, 202)

    def test_post_unlock_server(self):
        # Get api samples to unlock server request.
        self.test_post_lock_server()
        response = self._do_post('servers/%s/action' % self.uuid,
                                 'admin-actions-unlock-server', {})
        self.assertEqual(response.status, 202)

    def test_post_backup_server(self):
        # Get api samples to backup server request.
        def image_details(self, context, **kwargs):
            """This stub is specifically used on the backup action."""
            # NOTE(maurosr): I've added this simple stub cause backup action
            # was trapped in infinite loop during fetch image phase since the
            # fake Image Service always returns the same set of images
            return None

        self.stubs.Set(fake._FakeImageService, 'detail', image_details)

        response = self._do_post('servers/%s/action' % self.uuid,
                                 'admin-actions-backup-server', {})
        self.assertEqual(response.status, 202)

    def test_post_live_migrate_server(self):
        # Get api samples to server live migrate request.
        def fake_live_migration_src_check(self, context, instance_ref):
            """Skip live migration scheduler checks."""
            return

        def fake_live_migration_dest_check(self, context, instance_ref, dest):
            """Skip live migration scheduler checks."""
            return dest

        def fake_live_migration_common(self, context, instance_ref, dest):
            """Skip live migration scheduler checks."""
            return
        self.stubs.Set(driver.Scheduler, '_live_migration_src_check',
                       fake_live_migration_src_check)
        self.stubs.Set(driver.Scheduler, '_live_migration_dest_check',
                       fake_live_migration_dest_check)
        self.stubs.Set(driver.Scheduler, '_live_migration_common_check',
                       fake_live_migration_common)

        def fake_get_compute(context, host):
            service = dict(host=host,
                           binary='nova-compute',
                           topic='compute',
                           report_count=1,
                           updated_at='foo',
                           hypervisor_type='bar',
                           hypervisor_version='1',
                           disabled=False)
            return {'compute_node': [service]}
        self.stubs.Set(db, "service_get_by_compute_host", fake_get_compute)

        response = self._do_post('servers/%s/action' % self.uuid,
                                 'admin-actions-live-migrate',
                                 {'hostname': self.compute.host})
        self.assertEqual(response.status, 202)

    def test_post_reset_state(self):
        # get api samples to server reset state request.
        response = self._do_post('servers/%s/action' % self.uuid,
                                 'admin-actions-reset-server-state', {})
        self.assertEqual(response.status, 202)


class AdminActionsSamplesXmlTest(AdminActionsSamplesJsonTest):
    ctype = 'xml'


class ConsolesSampleJsonTests(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib"
                                     ".consoles.Consoles")

    def setUp(self):
        super(ConsolesSampleJsonTests, self).setUp()
        self.flags(vnc_enabled=True)
        self.flags(enabled=True, group='spice')

    def test_get_vnc_console(self):
        uuid = self._post_server()
        response = self._do_post('servers/%s/action' % uuid,
                                 'get-vnc-console-post-req',
                                {'action': 'os-getVNCConsole'})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs["url"] = \
            "((https?):((//)|(\\\\))+([\w\d:#@%/;$()~_?\+-=\\\.&](#!)?)*)"
        return self._verify_response('get-vnc-console-post-resp',
                                       subs, response)

    def test_get_spice_console(self):
        uuid = self._post_server()
        response = self._do_post('servers/%s/action' % uuid,
                                 'get-spice-console-post-req',
                                {'action': 'os-getSPICEConsole'})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs["url"] = \
            "((https?):((//)|(\\\\))+([\w\d:#@%/;$()~_?\+-=\\\.&](#!)?)*)"
        return self._verify_response('get-spice-console-post-resp',
                                       subs, response)


class ConsolesSampleXmlTests(ConsolesSampleJsonTests):
        ctype = 'xml'


class DeferredDeleteSampleJsonTests(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib"
                                     ".deferred_delete.Deferred_delete")

    def setUp(self):
        super(DeferredDeleteSampleJsonTests, self).setUp()
        self.flags(reclaim_instance_interval=1)

    def test_restore(self):
        uuid = self._post_server()
        response = self._do_delete('servers/%s' % uuid)

        response = self._do_post('servers/%s/action' % uuid,
                                 'restore-post-req', {})
        self.assertEqual(response.status, 202)
        self.assertEqual(response.read(), '')

    def test_force_delete(self):
        uuid = self._post_server()
        response = self._do_delete('servers/%s' % uuid)

        response = self._do_post('servers/%s/action' % uuid,
                                 'force-delete-post-req', {})
        self.assertEqual(response.status, 202)
        self.assertEqual(response.read(), '')


class DeferredDeleteSampleXmlTests(DeferredDeleteSampleJsonTests):
        ctype = 'xml'


class QuotasSampleJsonTests(ApiSampleTestBase):
    extension_name = "nova.api.openstack.compute.contrib.quotas.Quotas"

    def test_show_quotas(self):
        # Get api sample to show quotas.
        response = self._do_get('os-quota-sets/fake_tenant')
        self.assertEqual(response.status, 200)
        return self._verify_response('quotas-show-get-resp', {}, response)

    def test_show_quotas_defaults(self):
        # Get api sample to show quotas defaults.
        response = self._do_get('os-quota-sets/fake_tenant/defaults')
        self.assertEqual(response.status, 200)
        return self._verify_response('quotas-show-defaults-get-resp',
                                     {}, response)

    def test_update_quotas(self):
        # Get api sample to update quotas.
        response = self._do_put('os-quota-sets/fake_tenant',
                                'quotas-update-post-req',
                                {})
        self.assertEqual(response.status, 200)
        return self._verify_response('quotas-update-post-resp', {}, response)


class QuotasSampleXmlTests(QuotasSampleJsonTests):
    ctype = "xml"


class ExtendedIpsSampleJsonTests(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib"
                      ".extended_ips.Extended_ips")

    def test_show(self):
        uuid = self._post_server()
        response = self._do_get('servers/%s' % uuid)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        subs['id'] = uuid
        subs['hypervisor_hostname'] = r'[\w\.\-]+'
        return self._verify_response('server-get-resp', subs, response)

    def test_detail(self):
        uuid = self._post_server()
        response = self._do_get('servers/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['id'] = uuid
        subs['hostid'] = '[a-f0-9]+'
        return self._verify_response('servers-detail-resp', subs, response)


class ExtendedIpsSampleXmlTests(ExtendedIpsSampleJsonTests):
        ctype = 'xml'


class ExtendedStatusSampleJsonTests(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib"
                      ".extended_status.Extended_status")

    def test_show(self):
        uuid = self._post_server()
        response = self._do_get('servers/%s' % uuid)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        return self._verify_response('server-get-resp', subs, response)

    def test_detail(self):
        uuid = self._post_server()
        response = self._do_get('servers/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['id'] = uuid
        subs['hostid'] = '[a-f0-9]+'
        return self._verify_response('servers-detail-resp', subs, response)


class ExtendedStatusSampleXmlTests(ExtendedStatusSampleJsonTests):
        ctype = 'xml'


class FlavorManageSampleJsonTests(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.flavormanage."
                      "Flavormanage")

    def _create_flavor(self):
        """Create a flavor."""
        subs = {
            'flavor_id': 10,
            'flavor_name': "test_flavor"
        }
        response = self._do_post("flavors",
                                 "flavor-create-post-req",
                                 subs)
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response("flavor-create-post-resp", subs, response)

    def test_create_flavor(self):
        # Get api sample to create a flavor.
        self._create_flavor()

    def test_delete_flavor(self):
        # Get api sample to delete a flavor.
        self._create_flavor()
        response = self._do_delete("flavors/10")
        self.assertEqual(response.status, 202)
        self.assertEqual(response.read(), '')


class FlavorManageSampleXmlTests(FlavorManageSampleJsonTests):
    ctype = "xml"


class ServerPasswordSampleJsonTests(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib.server_password."
                      "Server_password")

    def test_get_password(self):

        # Mock password since there is no api to set it
        def fake_ext_password(*args, **kwargs):
            return ("xlozO3wLCBRWAa2yDjCCVx8vwNPypxnypmRYDa/zErlQ+EzPe1S/"
                    "Gz6nfmC52mOlOSCRuUOmG7kqqgejPof6M7bOezS387zjq4LSvvwp"
                    "28zUknzy4YzfFGhnHAdai3TxUJ26pfQCYrq8UTzmKF2Bq8ioSEtV"
                    "VzM0A96pDh8W2i7BOz6MdoiVyiev/I1K2LsuipfxSJR7Wdke4zNX"
                    "JjHHP2RfYsVbZ/k9ANu+Nz4iIH8/7Cacud/pphH7EjrY6a4RZNrj"
                    "QskrhKYed0YERpotyjYk1eDtRe72GrSiXteqCM4biaQ5w3ruS+Ac"
                    "X//PXk3uJ5kC7d67fPXaVz4WaQRYMg==")
        self.stubs.Set(password, "extract_password", fake_ext_password)
        uuid = self._post_server()
        response = self._do_get('servers/%s/os-server-password' % uuid)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['encrypted_password'] = fake_ext_password().replace('+', '\\+')
        return self._verify_response('get-password-resp', subs, response)

    def test_reset_password(self):
        uuid = self._post_server()
        response = self._do_delete('servers/%s/os-server-password' % uuid)
        self.assertEqual(response.status, 204)


class ServerPasswordSampleXmlTests(ServerPasswordSampleJsonTests):
    ctype = "xml"


class DiskConfigJsonTest(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib.disk_config."
                      "Disk_config")

    def test_list_servers_detail(self):
        uuid = self._post_server()
        response = self._do_get('servers/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        subs['id'] = uuid
        return self._verify_response('list-servers-detail-get',
                                     subs, response)

    def test_get_server(self):
        uuid = self._post_server()
        response = self._do_get('servers/%s' % uuid)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        return self._verify_response('server-get-resp', subs, response)

    def test_update_server(self):
        uuid = self._post_server()
        response = self._do_put('servers/%s' % uuid,
                                'server-update-put-req', {})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        return self._verify_response('server-update-put-resp',
                                      subs, response)

    def test_resize_server(self):
        self.flags(allow_resize_to_same_host=True)
        uuid = self._post_server()
        response = self._do_post('servers/%s/action' % uuid,
                                 'server-resize-post-req', {})
        self.assertEqual(response.status, 202)
        # NOTE(tmello): Resize does not return response body
        # Bug #1085213.
        self.assertEqual(response.read(), "")

    def test_rebuild_server(self):
        uuid = self._post_server()
        subs = {
            'image_id': fake.get_valid_image_id(),
            'host': self._get_host(),
        }
        response = self._do_post('servers/%s/action' % uuid,
                                 'server-action-rebuild-req', subs)
        self.assertEqual(response.status, 202)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        return self._verify_response('server-action-rebuild-resp',
                                      subs, response)

    def test_get_image(self):
        image_id = fake.get_valid_image_id()
        response = self._do_get('images/%s' % image_id)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['image_id'] = image_id
        return self._verify_response('image-get-resp', subs, response)

    def test_list_images(self):
        response = self._do_get('images/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('image-list-resp', subs, response)


class DiskConfigXmlTest(DiskConfigJsonTest):
        ctype = 'xml'


class OsNetworksJsonTests(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.os_tenant_networks"
                      ".Os_tenant_networks")

    def setUp(self):
        super(OsNetworksJsonTests, self).setUp()
        CONF.set_override("enable_network_quota", True)

        def fake(*args, **kwargs):
            pass

        self.stubs.Set(nova.quota.QUOTAS, "reserve", fake)
        self.stubs.Set(nova.quota.QUOTAS, "commit", fake)
        self.stubs.Set(nova.quota.QUOTAS, "rollback", fake)
        self.stubs.Set(nova.quota.QuotaEngine, "reserve", fake)
        self.stubs.Set(nova.quota.QuotaEngine, "commit", fake)
        self.stubs.Set(nova.quota.QuotaEngine, "rollback", fake)

    def test_list_networks(self):
        response = self._do_get('os-tenant-networks')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('networks-list-res', subs, response)

    def test_create_network(self):
        response = self._do_post('os-tenant-networks', "networks-post-req", {})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        self._verify_response('networks-post-res', subs, response)

    def test_delete_network(self):
        response = self._do_post('os-tenant-networks', "networks-post-req", {})
        net = json.loads(response.read())
        response = self._do_delete('os-tenant-networks/%s' %
                                                net["network"]["id"])
        self.assertEqual(response.status, 202)


class NetworksJsonTests(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib"
                      ".os_networks.Os_networks")

    def setUp(self):
        super(NetworksJsonTests, self).setUp()
        fake_network_api = test_networks.FakeNetworkAPI()
        self.stubs.Set(network_api.API, "get_all",
                       fake_network_api.get_all)
        self.stubs.Set(network_api.API, "get",
                       fake_network_api.get)
        self.stubs.Set(network_api.API, "associate",
                       fake_network_api.associate)
        self.stubs.Set(network_api.API, "delete",
                       fake_network_api.delete)
        self.stubs.Set(network_api.API, "create",
                       fake_network_api.create)
        self.stubs.Set(network_api.API, "add_network_to_project",
                       fake_network_api.add_network_to_project)

    def test_network_list(self):
        response = self._do_get('os-networks')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        self._verify_response('networks-list-resp', subs, response)

    def test_network_disassociate(self):
        uuid = test_networks.FAKE_NETWORKS[0]['uuid']
        response = self._do_post('os-networks/%s/action' % uuid,
                                 'networks-disassociate-req', {})
        self.assertEqual(response.status, 202)

    def test_network_show(self):
        uuid = test_networks.FAKE_NETWORKS[0]['uuid']
        response = self._do_get('os-networks/%s' % uuid)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        self._verify_response('network-show-resp', subs, response)

    def test_network_create(self):
        response = self._do_post("os-networks",
                                 'network-create-req', {})
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        self._verify_response('network-create-resp', subs, response)

    def test_network_add(self):
        response = self._do_post("os-networks/add",
                                 'network-add-req', {})
        self.assertEqual(response.status, 202)


class NetworksXmlTests(NetworksJsonTests):
    ctype = 'xml'


class NetworksAssociateJsonTests(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib"
                                     ".networks_associate.Networks_associate")

    _sentinel = object()

    def _get_flags(self):
        f = super(NetworksAssociateJsonTests, self)._get_flags()
        f['osapi_compute_extension'] = CONF.osapi_compute_extension[:]
        # Networks_associate requires Networks to be update
        f['osapi_compute_extension'].append(
            'nova.api.openstack.compute.contrib.os_networks.Os_networks')
        return f

    def setUp(self):
        super(NetworksAssociateJsonTests, self).setUp()

        def fake_associate(self, context, network_id,
                           host=NetworksAssociateJsonTests._sentinel,
                           project=NetworksAssociateJsonTests._sentinel):
            return True

        self.stubs.Set(network_api.API, "associate", fake_associate)

    def test_disassociate(self):
        response = self._do_post('os-networks/1/action',
                                 'network-disassociate-req',
                                 {})
        self.assertEqual(response.status, 202)

    def test_disassociate_host(self):
        response = self._do_post('os-networks/1/action',
                                 'network-disassociate-host-req',
                                 {})
        self.assertEqual(response.status, 202)

    def test_disassociate_project(self):
        response = self._do_post('os-networks/1/action',
                                 'network-disassociate-project-req',
                                 {})
        self.assertEqual(response.status, 202)

    def test_associate_host(self):
        response = self._do_post('os-networks/1/action',
                                 'network-associate-host-req',
                                 {"host": "testHost"})
        self.assertEqual(response.status, 202)


class NetworksAssociateXmlTests(NetworksAssociateJsonTests):
    ctype = 'xml'


class FlavorDisabledSampleJsonTests(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.flavor_disabled."
                      "Flavor_disabled")

    def test_show_flavor(self):
        # Get api sample to show flavor_disabled attr. of a flavor.
        flavor_id = 1
        response = self._do_get('flavors/%s' % flavor_id)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['flavor_id'] = flavor_id
        return self._verify_response('flavor-show-get-resp', subs,
                                     response)

    def test_detail_flavor(self):
        # Get api sample to show details of a flavor.
        response = self._do_get('flavors/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('flavor-detail-get-resp', subs,
                                     response)


class FlavorDisabledSampleXmlTests(FlavorDisabledSampleJsonTests):
    ctype = "xml"


class QuotaClassesSampleJsonTests(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.quota_classes."
                      "Quota_classes")
    set_id = 'test_class'

    def test_show_quota_classes(self):
        # Get api sample to show quota classes.
        response = self._do_get('os-quota-class-sets/%s' % self.set_id)
        self.assertEqual(response.status, 200)
        subs = {'set_id': self.set_id}
        return self._verify_response('quota-classes-show-get-resp', subs,
                                     response)

    def test_update_quota_classes(self):
        # Get api sample to update quota classes.
        response = self._do_put('os-quota-class-sets/%s' % self.set_id,
                                'quota-classes-update-post-req',
                                {})
        self.assertEqual(response.status, 200)
        return self._verify_response('quota-classes-update-post-resp',
                                     {}, response)


class QuotaClassesSampleXmlTests(QuotaClassesSampleJsonTests):
    ctype = "xml"


class CellsSampleJsonTest(ApiSampleTestBase):
    extension_name = "nova.api.openstack.compute.contrib.cells.Cells"

    def setUp(self):
        # db_check_interval < 0 makes cells manager always hit the DB
        self.flags(enable=True, db_check_interval=-1, group='cells')
        super(CellsSampleJsonTest, self).setUp()
        self._stub_cells()

    def _stub_cells(self, num_cells=5):
        self.cells = []
        self.cells_next_id = 1

        def _fake_cell_get_all(context):
            return self.cells

        def _fake_cell_get(context, cell_name):
            for cell in self.cells:
                if cell['name'] == cell_name:
                    return cell
            raise exception.CellNotFound(cell_name=cell_name)

        for x in xrange(num_cells):
            cell = models.Cell()
            our_id = self.cells_next_id
            self.cells_next_id += 1
            cell.update({'id': our_id,
                         'name': 'cell%s' % our_id,
                         'username': 'username%s' % our_id,
                         'is_parent': our_id % 2 == 0})
            self.cells.append(cell)

        self.stubs.Set(db, 'cell_get_all', _fake_cell_get_all)
        self.stubs.Set(db, 'cell_get', _fake_cell_get)

    def test_cells_empty_list(self):
        # Override this
        self._stub_cells(num_cells=0)
        response = self._do_get('os-cells')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('cells-list-empty-resp', subs, response)

    def test_cells_list(self):
        response = self._do_get('os-cells')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('cells-list-resp', subs, response)

    def test_cells_get(self):
        response = self._do_get('os-cells/cell3')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('cells-get-resp', subs, response)


class CellsSampleXmlTest(CellsSampleJsonTest):
    ctype = 'xml'


class BareMetalNodesJsonTest(ApiSampleTestBase, bm_db_base.BMDBTestCase):
    extension_name = ('nova.api.openstack.compute.contrib.baremetal_nodes.'
                      'Baremetal_nodes')

    def _create_node(self):
        response = self._do_post("os-baremetal-nodes",
                                 "baremetal-node-create-req",
                                 {})
        self.assertEqual(response.status, 200)
        subs = {'node_id': '(?P<id>\d+)'}
        return self._verify_response("baremetal-node-create-resp",
                                     subs, response)

    def test_create_node(self):
        self._create_node()

    def test_list_nodes(self):
        node_id = self._create_node()
        interface_id = self._add_interface(node_id)
        response = self._do_get('os-baremetal-nodes')
        self.assertEqual(response.status, 200)
        subs = {'node_id': node_id,
                'interface_id': interface_id,
                'address': 'aa:aa:aa:aa:aa:aa',
                }
        return self._verify_response('baremetal-node-list-resp',
                                     subs, response)

    def test_show_node(self):
        node_id = self._create_node()
        interface_id = self._add_interface(node_id)
        response = self._do_get('os-baremetal-nodes/%s' % node_id)
        self.assertEqual(response.status, 200)
        subs = {'node_id': node_id,
                'interface_id': interface_id,
                'address': 'aa:aa:aa:aa:aa:aa',
                }
        return self._verify_response('baremetal-node-show-resp',
                                     subs, response)

    def test_delete_node(self):
        node_id = self._create_node()
        response = self._do_delete("os-baremetal-nodes/%s" % node_id)
        self.assertEqual(response.status, 202)

    def _add_interface(self, node_id):
        response = self._do_post("os-baremetal-nodes/%s/action" % node_id,
                                 "baremetal-node-add-interface-req",
                                 {'address': 'aa:aa:aa:aa:aa:aa'})
        self.assertEqual(response.status, 200)
        subs = {'interface_id': r'(?P<id>\d+)'}
        return self._verify_response("baremetal-node-add-interface-resp",
                                     subs, response)

    def test_add_interface(self):
        node_id = self._create_node()
        self._add_interface(node_id)

    def test_remove_interface(self):
        node_id = self._create_node()
        self._add_interface(node_id)
        response = self._do_post("os-baremetal-nodes/%s/action" % node_id,
                                 "baremetal-node-remove-interface-req",
                                 {'address': 'aa:aa:aa:aa:aa:aa'})
        self.assertEqual(response.status, 202)
        self.assertEqual(response.read(), "")


class BareMetalNodesXmlTest(BareMetalNodesJsonTest):
    ctype = 'xml'


class FloatingIPPoolsSampleJsonTests(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.floating_ip_pools."
                      "Floating_ip_pools")

    def test_list_floatingippools(self):
        pool_list = ["pool1", "pool2"]

        def fake_get_floating_ip_pools(self, context):
            return [{'name': pool_list[0]},
                    {'name': pool_list[1]}]

        self.stubs.Set(network_api.API, "get_floating_ip_pools",
                       fake_get_floating_ip_pools)
        response = self._do_get('os-floating-ip-pools')
        self.assertEqual(response.status, 200)
        subs = {
            'pool1': pool_list[0],
            'pool2': pool_list[1]
        }
        return self._verify_response('floatingippools-list-resp',
                                     subs, response)


class FloatingIPPoolsSampleXmlTests(FloatingIPPoolsSampleJsonTests):
    ctype = 'xml'


class MultinicSampleJsonTest(ServersSampleBase):
    extension_name = "nova.api.openstack.compute.contrib.multinic.Multinic"

    def setUp(self):
        super(MultinicSampleJsonTest, self).setUp()
        self.uuid = self._post_server()

    def test_add_fixed_ip(self):
        subs = {"networkId": 1}
        response = self._do_post('servers/%s/action' % (self.uuid),
                                 'multinic-add-fixed-ip-req', subs)
        self.assertEqual(response.status, 202)

    def test_remove_fixed_ip(self):
        subs = {"ip": "10.0.0.2"}
        response = self._do_post('servers/%s/action' % (self.uuid),
                                 'multinic-remove-fixed-ip-req', subs)
        self.assertEqual(response.status, 202)


class MultinicSampleXmlTest(MultinicSampleJsonTest):
    ctype = "xml"


class InstanceUsageAuditLogJsonTest(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib."
                      "instance_usage_audit_log.Instance_usage_audit_log")

    def test_show_instance_usage_audit_log(self):
        response = self._do_get('os-instance_usage_audit_log/%s' %
                                urllib.quote('2012-07-05 10:00:00'))
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        return self._verify_response('inst-usage-audit-log-show-get-resp',
                                     subs, response)

    def test_index_instance_usage_audit_log(self):
        response = self._do_get('os-instance_usage_audit_log')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        return self._verify_response('inst-usage-audit-log-index-get-resp',
                                     subs, response)


class InstanceUsageAuditLogXmlTest(InstanceUsageAuditLogJsonTest):
    ctype = "xml"


class FlavorExtraSpecsSampleJsonTests(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.flavorextraspecs."
                      "Flavorextraspecs")

    def _flavor_extra_specs_create(self):
        subs = {'value1': 'value1',
                'value2': 'value2'
        }
        response = self._do_post('flavors/1/os-extra_specs',
                                 'flavor-extra-specs-create-req', subs)
        self.assertEqual(response.status, 200)
        return self._verify_response('flavor-extra-specs-create-resp',
                                     subs, response)

    def test_flavor_extra_specs_get(self):
        subs = {'value1': 'value1'}
        self._flavor_extra_specs_create()
        response = self._do_get('flavors/1/os-extra_specs/key1')
        self.assertEqual(response.status, 200)
        return self._verify_response('flavor-extra-specs-get-resp',
                                     subs, response)

    def test_flavor_extra_specs_list(self):
        subs = {'value1': 'value1',
                'value2': 'value2'
        }
        self._flavor_extra_specs_create()
        response = self._do_get('flavors/1/os-extra_specs')
        self.assertEqual(response.status, 200)
        return self._verify_response('flavor-extra-specs-list-resp',
                                     subs, response)

    def test_flavor_extra_specs_create(self):
        return self._flavor_extra_specs_create()

    def test_flavor_extra_specs_update(self):
        subs = {'value1': 'new_value1'}
        self._flavor_extra_specs_create()
        response = self._do_put('flavors/1/os-extra_specs/key1',
                                'flavor-extra-specs-update-req', subs)
        self.assertEqual(response.status, 200)
        return self._verify_response('flavor-extra-specs-update-resp',
                                     subs, response)

    def test_flavor_extra_specs_delete(self):
        self._flavor_extra_specs_create()
        response = self._do_delete('flavors/1/os-extra_specs/key1')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), '')


class FlavorExtraSpecsSampleXmlTests(FlavorExtraSpecsSampleJsonTests):
    ctype = 'xml'


class FpingSampleJsonTests(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib.fping.Fping")

    def setUp(self):
        super(FpingSampleJsonTests, self).setUp()

        def fake_check_fping(self):
            pass
        self.stubs.Set(utils, "execute", test_fping.execute)
        self.stubs.Set(fping.FpingController, "check_fping",
                       fake_check_fping)

    def test_get_fping(self):
        self._post_server()
        response = self._do_get('os-fping')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('fping-get-resp', subs, response)

    def test_get_fping_details(self):
        uuid = self._post_server()
        response = self._do_get('os-fping/%s' % (uuid))
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('fping-get-details-resp', subs, response)


class FpingSampleXmlTests(FpingSampleJsonTests):
    ctype = 'xml'


class ExtendedAvailabilityZoneJsonTests(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib"
                                ".extended_availability_zone"
                                ".Extended_availability_zone")

    def test_show(self):
        uuid = self._post_server()
        response = self._do_get('servers/%s' % uuid)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        return self._verify_response('server-get-resp', subs, response)

    def test_detail(self):
        uuid = self._post_server()
        response = self._do_get('servers/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        return self._verify_response('servers-detail-resp', subs, response)


class ExtendedAvailabilityZoneXmlTests(ExtendedAvailabilityZoneJsonTests):
    ctype = 'xml'


class EvacuateJsonTest(ServersSampleBase):

    extension_name = ("nova.api.openstack.compute.contrib"
                      ".evacuate.Evacuate")

    def test_server_evacuate(self):
        uuid = self._post_server()

        req_subs = {
            'host': 'TargetHost',
            "adminPass": "MySecretPass",
            "onSharedStorage": 'False'
        }

        def fake_service_is_up(self, service):
            """Simulate validation of instance host is down."""
            return False

        self.stubs.Set(service_group_api.API, 'service_is_up',
                       fake_service_is_up)

        response = self._do_post('servers/%s/action' % uuid,
                                 'server-evacuate-req', req_subs)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('server-evacuate-resp', subs,
                                     response)


class EvacuateXmlTest(EvacuateJsonTest):
    ctype = 'xml'


class FloatingIpDNSJsonTest(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.floating_ip_dns."
                      "Floating_ip_dns")

    domain = 'domain1.example.org'
    name = 'instance1'
    scope = 'public'
    project = 'project1'
    dns_type = 'A'
    ip = '192.168.1.1'

    def _create_or_update(self):
        subs = {'domain': self.domain,
                'project': self.project,
                'scope': self.scope}
        response = self._do_put('os-floating-ip-dns/%s' % self.domain,
                                'floating-ip-dns-create-or-update-req', subs)
        self.assertEqual(response.status, 200)
        self._verify_response('floating-ip-dns-create-or-update-resp', subs,
                              response)

    def _create_or_update_entry(self):
        subs = {'ip': self.ip, 'dns_type': self.dns_type}
        response = self._do_put('os-floating-ip-dns/%s/entries/%s'
                                % (self.domain, self.name),
                                'floating-ip-dns-create-or-update-entry-req',
                                subs)
        self.assertEqual(response.status, 200)
        subs.update({'name': self.name, 'domain': self.domain})
        self._verify_response('floating-ip-dns-create-or-update-entry-resp',
                              subs, response)

    def test_floating_ip_dns_list(self):
        self._create_or_update()
        response = self._do_get('os-floating-ip-dns')
        self.assertEqual(response.status, 200)
        subs = {'domain': self.domain,
                'project': self.project,
                'scope': self.scope}
        return self._verify_response('floating-ip-dns-list-resp', subs,
                                     response)

    def test_floating_ip_dns_create_or_update(self):
        self._create_or_update()

    def test_floating_ip_dns_delete(self):
        self._create_or_update()
        response = self._do_delete('os-floating-ip-dns/%s' % self.domain)
        self.assertEqual(response.status, 202)

    def test_floating_ip_dns_create_or_update_entry(self):
        self._create_or_update_entry()

    def test_floating_ip_dns_entry_get(self):
        self._create_or_update_entry()
        response = self._do_get('os-floating-ip-dns/%s/entries/%s'
                                % (self.domain, self.name))
        self.assertEqual(response.status, 200)
        subs = {'domain': self.domain,
                'ip': self.ip,
                'name': self.name}
        return self._verify_response('floating-ip-dns-entry-get-resp', subs,
                                     response)

    def test_floating_ip_dns_entry_delete(self):
        self._create_or_update_entry()
        response = self._do_delete('os-floating-ip-dns/%s/entries/%s'
                                   % (self.domain, self.name))
        self.assertEqual(response.status, 202)

    def test_floating_ip_dns_entry_list(self):
        self._create_or_update_entry()
        response = self._do_get('os-floating-ip-dns/%s/entries/%s'
                                % (self.domain, self.ip))
        self.assertEqual(response.status, 200)
        subs = {'domain': self.domain,
                'ip': self.ip,
                'name': self.name}
        return self._verify_response('floating-ip-dns-entry-list-resp', subs,
                                     response)


class FloatingIpDNSXmlTest(FloatingIpDNSJsonTest):
    ctype = 'xml'


class InstanceActionsSampleJsonTest(ApiSampleTestBase):
    extension_name = ('nova.api.openstack.compute.contrib.instance_actions.'
                      'Instance_actions')

    def setUp(self):
        super(InstanceActionsSampleJsonTest, self).setUp()
        self.actions = fake_instance_actions.FAKE_ACTIONS
        self.events = fake_instance_actions.FAKE_EVENTS
        self.instance = test_utils.get_test_instance()

        def fake_instance_action_get_by_request_id(context, uuid, request_id):
            return copy.deepcopy(self.actions[uuid][request_id])

        def fake_instance_actions_get(context, uuid):
            return [copy.deepcopy(value) for value in
                    self.actions[uuid].itervalues()]

        def fake_instance_action_events_get(context, action_id):
            return copy.deepcopy(self.events[action_id])

        def fake_instance_get_by_uuid(context, instance_id):
            return self.instance

        def fake_get(self, context, instance_uuid):
            return {'uuid': instance_uuid}

        self.stubs.Set(db, 'action_get_by_request_id',
                       fake_instance_action_get_by_request_id)
        self.stubs.Set(db, 'actions_get', fake_instance_actions_get)
        self.stubs.Set(db, 'action_events_get',
                       fake_instance_action_events_get)
        self.stubs.Set(db, 'instance_get_by_uuid', fake_instance_get_by_uuid)
        self.stubs.Set(compute_api.API, 'get', fake_get)

    def test_instance_action_get(self):
        fake_uuid = fake_instance_actions.FAKE_UUID
        fake_request_id = fake_instance_actions.FAKE_REQUEST_ID1
        fake_action = self.actions[fake_uuid][fake_request_id]

        response = self._do_get('servers/%s/os-instance-actions/%s' %
                                (fake_uuid, fake_request_id))
        subs = self._get_regexes()
        subs['action'] = '(reboot)|(resize)'
        subs['instance_uuid'] = fake_uuid
        subs['integer_id'] = '[0-9]+'
        subs['request_id'] = fake_action['request_id']
        subs['start_time'] = fake_action['start_time']
        subs['result'] = '(Success)|(Error)'
        subs['event'] = '(schedule)|(compute_create)'
        return self._verify_response('instance-action-get-resp', subs,
                                     response)

    def test_instance_actions_list(self):
        fake_uuid = fake_instance_actions.FAKE_UUID
        response = self._do_get('servers/%s/os-instance-actions' % (fake_uuid))
        subs = self._get_regexes()
        subs['action'] = '(reboot)|(resize)'
        subs['integer_id'] = '[0-9]+'
        subs['request_id'] = ('req-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}'
                              '-[0-9a-f]{4}-[0-9a-f]{12}')
        return self._verify_response('instance-actions-list-resp', subs,
                                     response)


class InstanceActionsSampleXmlTest(InstanceActionsSampleJsonTest):
        ctype = 'xml'


class ImageSizeSampleJsonTests(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib"
                      ".image_size.Image_size")

    def test_show(self):
        # Get api sample of one single image details request.
        image_id = fake.get_valid_image_id()
        response = self._do_get('images/%s' % image_id)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['image_id'] = image_id
        return self._verify_response('image-get-resp', subs, response)

    def test_detail(self):
        # Get api sample of all images details request.
        response = self._do_get('images/detail')
        subs = self._get_regexes()
        return self._verify_response('images-details-get-resp', subs, response)


class ImageSizeSampleXmlTests(ImageSizeSampleJsonTests):
        ctype = 'xml'


class ConfigDriveSampleJsonTest(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib.config_drive."
                      "Config_drive")

    def setUp(self):
        super(ConfigDriveSampleJsonTest, self).setUp()
        fakes.stub_out_networking(self.stubs)
        fakes.stub_out_rate_limiting(self.stubs)
        fake.stub_out_image_service(self.stubs)

    def test_config_drive_show(self):
        uuid = self._post_server()
        response = self._do_get('servers/%s' % uuid)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        # config drive can be an uuid or empty value
        subs['cdrive'] = '(%s)?' % subs['uuid']
        return self._verify_response('server-config-drive-get-resp', subs,
                                     response)

    def test_config_drive_detail(self):
        uuid = self._post_server()
        response = self._do_get('servers/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs['hostid'] = '[a-f0-9]+'
        # config drive can be an uuid or empty value
        subs['cdrive'] = '(%s)?' % subs['uuid']
        return self._verify_response('servers-config-drive-details-resp',
                                     subs, response)


class ConfigDriveSampleXmlTest(ConfigDriveSampleJsonTest):
    ctype = 'xml'


class FlavorAccessSampleJsonTests(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.flavor_access."
                      "Flavor_access")

    def _get_flags(self):
        f = super(FlavorAccessSampleJsonTests, self)._get_flags()
        f['osapi_compute_extension'] = CONF.osapi_compute_extension[:]
        # FlavorAccess extension also needs Flavormanage to be loaded.
        f['osapi_compute_extension'].append(
            'nova.api.openstack.compute.contrib.flavormanage.Flavormanage')
        return f

    def _add_tenant(self):
        subs = {
            'tenant_id': 'fake_tenant',
            'flavor_id': 10
        }
        response = self._do_post('flavors/10/action',
                                 'flavor-access-add-tenant-req',
                                 subs)
        self.assertEqual(response.status, 200)
        return self._verify_response('flavor-access-add-tenant-resp',
                                     subs, response)

    def _create_flavor(self):
        subs = {
            'flavor_id': 10,
            'flavor_name': 'test_flavor'
        }
        response = self._do_post("flavors",
                                 "flavor-access-create-req",
                                 subs)
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response("flavor-access-create-resp",
                                     subs, response)

    def test_flavor_access_create(self):
        self._create_flavor()

    def test_flavor_access_detail(self):
        response = self._do_get('flavors/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('flavor-access-detail-resp',
                                     subs, response)

    def test_flavor_access_list(self):
        self._create_flavor()
        self._add_tenant()
        flavor_id = 10
        response = self._do_get('flavors/%s/os-flavor-access' % flavor_id)
        self.assertEqual(response.status, 200)
        subs = {
            'flavor_id': flavor_id,
            'tenant_id': 'fake_tenant',
        }
        return self._verify_response('flavor-access-list-resp',
                                     subs, response)

    def test_flavor_access_show(self):
        flavor_id = 1
        response = self._do_get('flavors/%s' % flavor_id)
        self.assertEqual(response.status, 200)
        subs = {
            'flavor_id': flavor_id
        }
        subs.update(self._get_regexes())
        return self._verify_response('flavor-access-show-resp',
                                     subs, response)

    def test_flavor_access_add_tenant(self):
        self._create_flavor()
        response = self._add_tenant()

    def test_flavor_access_remove_tenant(self):
        self._create_flavor()
        self._add_tenant()
        subs = {
            'tenant_id': 'fake_tenant',
        }
        response = self._do_post('flavors/10/action',
                                 "flavor-access-remove-tenant-req",
                                 subs)
        self.assertEqual(response.status, 200)
        return self._verify_response('flavor-access-remove-tenant-resp',
                                     {}, response)


class FlavorAccessSampleXmlTests(FlavorAccessSampleJsonTests):
    ctype = 'xml'


class HypervisorsSampleJsonTests(ApiSampleTestBase):
    extension_name = ("nova.api.openstack.compute.contrib.hypervisors."
                      "Hypervisors")

    def test_hypervisors_list(self):
        response = self._do_get('os-hypervisors')
        self.assertEqual(response.status, 200)
        return self._verify_response('hypervisors-list-resp',
                                     {}, response)

    def test_hypervisors_search(self):
        response = self._do_get('os-hypervisors/fake/search')
        self.assertEqual(response.status, 200)
        return self._verify_response('hypervisors-search-resp',
                                     {}, response)

    def test_hypervisors_servers(self):
        response = self._do_get('os-hypervisors/fake/servers')
        self.assertEqual(response.status, 200)
        return self._verify_response('hypervisors-servers-resp',
                                     {}, response)

    def test_hypervisors_show(self):
        hypervisor_id = 1
        subs = {
            'hypervisor_id': hypervisor_id
        }
        response = self._do_get('os-hypervisors/%s' % hypervisor_id)
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response('hypervisors-show-resp',
                                     subs, response)

    def test_hypervisors_statistics(self):
        response = self._do_get('os-hypervisors/statistics')
        self.assertEqual(response.status, 200)
        return self._verify_response('hypervisors-statistics-resp',
                                     {}, response)

    def test_hypervisors_uptime(self):
        def fake_get_host_uptime(self, context, hyp):
            return (" 08:32:11 up 93 days, 18:25, 12 users,  load average:"
                    " 0.20, 0.12, 0.14")

        self.stubs.Set(compute_api.HostAPI,
                       'get_host_uptime', fake_get_host_uptime)
        hypervisor_id = 1
        response = self._do_get('os-hypervisors/%s/uptime' % hypervisor_id)
        self.assertEqual(response.status, 200)
        subs = {
            'hypervisor_id': hypervisor_id,
        }
        return self._verify_response('hypervisors-uptime-resp',
                                     subs, response)


class HypervisorsSampleXmlTests(HypervisorsSampleJsonTests):
    ctype = "xml"


class AttachInterfacesSampleJsonTest(ServersSampleBase):
    extension_name = ('nova.api.openstack.compute.contrib.attach_interfaces.'
                      'Attach_interfaces')

    def setUp(self):
        super(AttachInterfacesSampleJsonTest, self).setUp()

        def fake_list_ports(self, *args, **kwargs):
            uuid = kwargs.get('device_id', None)
            if not uuid:
                raise exception.InstanceNotFound(instance_id=None)
            port_data = {
                "id": "ce531f90-199f-48c0-816c-13e38010b442",
                "network_id": "3cb9bc59-5699-4588-a4b1-b87f96708bc6",
                "admin_state_up": True,
                "status": "ACTIVE",
                "mac_address": "fa:16:3e:4c:2c:30",
                "fixed_ips": [
                    {
                        "ip_address": "192.168.1.3",
                        "subnet_id": "f8a6e8f8-c2ec-497c-9f23-da9616de54ef"
                    }
                ],
                "device_id": uuid,
                }
            ports = {'ports': [port_data]}
            return ports

        def fake_show_port(self, context, port_id=None):
            if not port_id:
                raise exception.PortNotFound(port_id=None)
            port_data = {
                "id": port_id,
                "network_id": "3cb9bc59-5699-4588-a4b1-b87f96708bc6",
                "admin_state_up": True,
                "status": "ACTIVE",
                "mac_address": "fa:16:3e:4c:2c:30",
                "fixed_ips": [
                    {
                        "ip_address": "192.168.1.3",
                        "subnet_id": "f8a6e8f8-c2ec-497c-9f23-da9616de54ef"
                    }
                ],
                "device_id": 'bece68a3-2f8b-4e66-9092-244493d6aba7',
                }
            port = {'port': port_data}
            return port

        def fake_attach_interface(self, context, instance,
                                  network_id, port_id,
                                  requested_ip='192.168.1.3'):
            if not network_id:
                network_id = "fake_net_uuid"
            if not port_id:
                port_id = "fake_port_uuid"
            network_info = [
                {
                    'bridge': 'br-100',
                    'id': network_id,
                    'cidr': '192.168.1.0/24',
                    'vlan': '101',
                    'injected': 'False',
                    'multi_host': 'False',
                    'bridge_interface': 'bridge_interface'
                },
                {
                    "vif_uuid": port_id,
                    "network_id": network_id,
                    "admin_state_up": True,
                    "status": "ACTIVE",
                    "mac_address": "fa:16:3e:4c:2c:30",
                    "fixed_ips": [
                        {
                            "ip_address": requested_ip,
                            "subnet_id": "f8a6e8f8-c2ec-497c-9f23-da9616de54ef"
                        }
                    ],
                    "device_id": instance['uuid'],
                }
            ]
            return network_info

        def fake_detach_interface(self, context, instance, port_id):
            pass

        self.stubs.Set(network_api.API, 'list_ports', fake_list_ports)
        self.stubs.Set(network_api.API, 'show_port', fake_show_port)
        self.stubs.Set(compute_api.API, 'attach_interface',
                       fake_attach_interface)
        self.stubs.Set(compute_api.API, 'detach_interface',
                       fake_detach_interface)
        self.flags(quantum_auth_strategy=None)
        self.flags(quantum_url='http://anyhost/')
        self.flags(quantum_url_timeout=30)

    def generalize_subs(self, subs, vanilla_regexes):
        subs['subnet_id'] = vanilla_regexes['uuid']
        subs['net_id'] = vanilla_regexes['uuid']
        subs['port_id'] = vanilla_regexes['uuid']
        subs['mac_addr'] = '(?:[a-f0-9]{2}:){5}[a-f0-9]{2}'
        subs['ip_address'] = vanilla_regexes['ip']
        return subs

    def test_list_interfaces(self):
        instance_uuid = self._post_server()
        response = self._do_get('servers/%s/os-interface' % instance_uuid)
        self.assertEqual(response.status, 200)
        subs = {
                'ip_address': '192.168.1.3',
                'subnet_id': 'f8a6e8f8-c2ec-497c-9f23-da9616de54ef',
                'mac_addr': 'fa:16:3e:4c:2c:30',
                'net_id': '3cb9bc59-5699-4588-a4b1-b87f96708bc6',
                'port_id': 'ce531f90-199f-48c0-816c-13e38010b442',
                'port_state': 'ACTIVE'
                }
        self._verify_response('attach-interfaces-list-resp', subs, response)

    def _stub_show_for_instance(self, instance_uuid, port_id):
        show_port = network_api.API().show_port(None, port_id)
        show_port['port']['device_id'] = instance_uuid
        self.stubs.Set(network_api.API, 'show_port', lambda *a, **k: show_port)

    def test_show_interfaces(self):
        instance_uuid = self._post_server()
        port_id = 'ce531f90-199f-48c0-816c-13e38010b442'
        self._stub_show_for_instance(instance_uuid, port_id)
        response = self._do_get('servers/%s/os-interface/%s' %
                                (instance_uuid, port_id))
        self.assertEqual(response.status, 200)
        subs = {
                'ip_address': '192.168.1.3',
                'subnet_id': 'f8a6e8f8-c2ec-497c-9f23-da9616de54ef',
                'mac_addr': 'fa:16:3e:4c:2c:30',
                'net_id': '3cb9bc59-5699-4588-a4b1-b87f96708bc6',
                'port_id': port_id,
                'port_state': 'ACTIVE'
                }
        self._verify_response('attach-interfaces-show-resp', subs, response)

    def test_create_interfaces(self, instance_uuid=None):
        if instance_uuid is None:
            instance_uuid = self._post_server()
        subs = {
                'net_id': '3cb9bc59-5699-4588-a4b1-b87f96708bc6',
                'port_id': 'ce531f90-199f-48c0-816c-13e38010b442',
                'subnet_id': 'f8a6e8f8-c2ec-497c-9f23-da9616de54ef',
                'ip_address': '192.168.1.3',
                'port_state': 'ACTIVE',
                'mac_addr': 'fa:16:3e:4c:2c:30',
                }
        self._stub_show_for_instance(instance_uuid, subs['port_id'])
        response = self._do_post('servers/%s/os-interface' % instance_uuid,
                                 'attach-interfaces-create-req', subs)
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        self._verify_response('attach-interfaces-create-resp',
                              subs, response)

    def test_delete_interfaces(self):
        instance_uuid = self._post_server()
        port_id = 'ce531f90-199f-48c0-816c-13e38010b442'
        response = self._do_delete('servers/%s/os-interface/%s' %
                                (instance_uuid, port_id))
        self.assertEqual(response.status, 202)
        self.assertEqual(response.read(), '')


class AttachInterfacesSampleXmlTest(AttachInterfacesSampleJsonTest):
    ctype = 'xml'


class SnapshotsSampleJsonTests(ApiSampleTestBase):
    extension_name = "nova.api.openstack.compute.contrib.volumes.Volumes"

    create_subs = {
            'snapshot_name': 'snap-001',
            'description': 'Daily backup',
            'volume_id': '521752a6-acf6-4b2d-bc7a-119f9148cd8c'
    }

    def setUp(self):
        super(SnapshotsSampleJsonTests, self).setUp()
        self.stubs.Set(cinder.API, "get_all_snapshots",
                       fakes.stub_snapshot_get_all)
        self.stubs.Set(cinder.API, "get_snapshot", fakes.stub_snapshot_get)

    def _create_snapshot(self):
        self.stubs.Set(cinder.API, "create_snapshot",
                       fakes.stub_snapshot_create)
        self.stubs.Set(cinder.API, "get", fakes.stub_volume_get)

        response = self._do_post("os-snapshots",
                                 "snapshot-create-req",
                                 self.create_subs)
        return response

    def test_snapshots_create(self):
        response = self._create_snapshot()
        self.assertEqual(response.status, 200)
        self.create_subs.update(self._get_regexes())
        return self._verify_response("snapshot-create-resp",
                                     self.create_subs, response)

    def test_snapshots_delete(self):
        self.stubs.Set(cinder.API, "delete_snapshot",
                       fakes.stub_snapshot_delete)
        self._create_snapshot()
        response = self._do_delete('os-snapshots/100')
        self.assertEqual(response.status, 202)
        self.assertEqual(response.read(), '')

    def test_snapshots_detail(self):
        response = self._do_get('os-snapshots/detail')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('snapshots-detail-resp',
                                     subs, response)

    def test_snapshots_list(self):
        response = self._do_get('os-snapshots')
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        return self._verify_response('snapshots-list-resp',
                                     subs, response)

    def test_snapshots_show(self):
        response = self._do_get('os-snapshots/100')
        self.assertEqual(response.status, 200)
        subs = {
            'snapshot_name': 'Default name',
            'description': 'Default description'
        }
        subs.update(self._get_regexes())
        return self._verify_response('snapshots-show-resp',
                                     subs, response)


class SnapshotsSampleXmlTests(SnapshotsSampleJsonTests):
    ctype = "xml"


class VolumeAttachmentsSampleJsonTest(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib.volumes.Volumes")

    def test_attach_volume_to_server(self):
        device_name = '/dev/vdd'
        self.stubs.Set(cinder.API, 'get', fakes.stub_volume_get)
        self.stubs.Set(cinder.API, 'check_attach', lambda *a, **k: None)
        self.stubs.Set(cinder.API, 'reserve_volume', lambda *a, **k: None)
        self.stubs.Set(compute_manager.ComputeManager,
                       "reserve_block_device_name",
                       lambda *a, **k: device_name)

        volume = fakes.stub_volume_get(None, context.get_admin_context(),
                                       'a26887c6-c47b-4654-abb5-dfadf7d3f803')
        subs = {
            'volume_id': volume['id'],
            'device': device_name
        }
        server_id = self._post_server()
        response = self._do_post('servers/%s/os-volume_attachments'
                                 % server_id,
                                 'attach-volume-to-server-req', subs)

        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        self._verify_response('attach-volume-to-server-resp',
                              subs, response)

    def _stub_compute_api_get_instance_bdms(self, server_id):

        def fake_compute_api_get_instance_bdms(self, context, instance):
            bdms = [
                {'volume_id': 'a26887c6-c47b-4654-abb5-dfadf7d3f803',
                'instance_uuid': server_id,
                'device_name': '/dev/sdd'},
                {'volume_id': 'a26887c6-c47b-4654-abb5-dfadf7d3f804',
                'instance_uuid': server_id,
                'device_name': '/dev/sdc'}
            ]
            return bdms

        self.stubs.Set(compute_api.API, "get_instance_bdms",
                       fake_compute_api_get_instance_bdms)

    def _stub_compute_api_get(self):

        def fake_compute_api_get(self, context, instance_id):
            return {'uuid': instance_id}

        self.stubs.Set(compute_api.API, 'get', fake_compute_api_get)

    def test_list_volume_attachments(self):
        server_id = self._post_server()

        self._stub_compute_api_get_instance_bdms(server_id)

        response = self._do_get('servers/%s/os-volume_attachments'
                                % server_id)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        self._verify_response('list-volume-attachments-resp',
                              subs, response)

    def test_volume_attachment_detail(self):
        server_id = self._post_server()
        attach_id = "a26887c6-c47b-4654-abb5-dfadf7d3f803"
        self._stub_compute_api_get_instance_bdms(server_id)
        self._stub_compute_api_get()
        response = self._do_get('servers/%s/os-volume_attachments/%s'
                                % (server_id, attach_id))
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        self._verify_response('volume-attachment-detail-resp',
                              subs, response)

    def test_volume_attachment_delete(self):
        server_id = self._post_server()
        attach_id = "a26887c6-c47b-4654-abb5-dfadf7d3f803"
        self._stub_compute_api_get_instance_bdms(server_id)
        self._stub_compute_api_get()
        self.stubs.Set(cinder.API, 'get', fakes.stub_volume_get)
        self.stubs.Set(compute_api.API, 'detach_volume', lambda *a, **k: None)
        response = self._do_delete('servers/%s/os-volume_attachments/%s'
                                   % (server_id, attach_id))
        self.assertEqual(response.status, 202)
        self.assertEqual(response.read(), '')


class VolumeAttachmentsSampleXmlTest(VolumeAttachmentsSampleJsonTest):
    ctype = 'xml'


class VolumesSampleJsonTest(ServersSampleBase):
    extension_name = ("nova.api.openstack.compute.contrib.volumes.Volumes")

    def _get_volume_id(self):
        return 'a26887c6-c47b-4654-abb5-dfadf7d3f803'

    def _stub_volume(self, id, displayname="Volume Name",
                     displaydesc="Volume Description", size=100):
        volume = {
                  'id': id,
                  'size': size,
                  'availability_zone': 'zone1:host1',
                  'instance_uuid': '3912f2b4-c5ba-4aec-9165-872876fe202e',
                  'mountpoint': '/',
                  'status': 'in-use',
                  'attach_status': 'attached',
                  'name': 'vol name',
                  'display_name': displayname,
                  'display_description': displaydesc,
                  'created_at': "2008-12-01T11:01:55",
                  'snapshot_id': None,
                  'volume_type_id': 'fakevoltype',
                  'volume_metadata': [],
                  'volume_type': {'name': 'Backup'}
                  }
        return volume

    def _stub_volume_get(self, context, volume_id):
        return self._stub_volume(volume_id)

    def _stub_volume_delete(self, context, *args, **param):
        pass

    def _stub_volume_get_all(self, context, search_opts=None):
        id = self._get_volume_id()
        return [self._stub_volume(id)]

    def _stub_volume_create(self, context, size, name, description, snapshot,
                       **param):
        id = self._get_volume_id()
        return self._stub_volume(id)

    def setUp(self):
        super(VolumesSampleJsonTest, self).setUp()
        fakes.stub_out_networking(self.stubs)
        fakes.stub_out_rate_limiting(self.stubs)

        self.stubs.Set(cinder.API, "delete", self._stub_volume_delete)
        self.stubs.Set(cinder.API, "get", self._stub_volume_get)
        self.stubs.Set(cinder.API, "get_all", self._stub_volume_get_all)

    def _post_volume(self):
        subs_req = {
                'volume_name': "Volume Name",
                'volume_desc': "Volume Description",
        }

        self.stubs.Set(cinder.API, "create", self._stub_volume_create)
        response = self._do_post('os-volumes', 'os-volumes-post-req',
                                 subs_req)
        self.assertEqual(response.status, 200)
        subs = self._get_regexes()
        subs.update(subs_req)
        return self._verify_response('os-volumes-post-resp', subs, response)

    def test_volumes_show(self):
        subs = {
                'volume_name': "Volume Name",
                'volume_desc': "Volume Description",
        }
        vol_id = self._get_volume_id()
        response = self._do_get('os-volumes/%s' % vol_id)
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response('os-volumes-get-resp', subs, response)

    def test_volumes_index(self):
        subs = {
                'volume_name': "Volume Name",
                'volume_desc': "Volume Description",
        }
        response = self._do_get('os-volumes')
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response('os-volumes-index-resp', subs, response)

    def test_volumes_detail(self):
        # For now, index and detail are the same.
        # See the volumes api
        subs = {
                'volume_name': "Volume Name",
                'volume_desc': "Volume Description",
        }
        response = self._do_get('os-volumes/detail')
        self.assertEqual(response.status, 200)
        subs.update(self._get_regexes())
        return self._verify_response('os-volumes-detail-resp', subs, response)

    def test_volumes_create(self):
        return self._post_volume()

    def test_volumes_delete(self):
        self._post_volume()
        vol_id = self._get_volume_id()
        response = self._do_delete('os-volumes/%s' % vol_id)
        self.assertEqual(response.status, 202)
        self.assertEqual(response.read(), '')


class VolumesSampleXmlTest(VolumesSampleJsonTest):
    ctype = 'xml'
