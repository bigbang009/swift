#!/usr/bin/python -u
# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import errno
import json
from contextlib import contextmanager
from hashlib import md5
import unittest
import uuid
import shutil
import random
from collections import defaultdict
import os

from test.probe.common import ECProbeTest

from swift.common import direct_client
from swift.common.storage_policy import EC_POLICY
from swift.common.manager import Manager
from swift.obj.reconstructor import _get_partners

from swiftclient import client, ClientException


class Body(object):

    def __init__(self, total=3.5 * 2 ** 20):
        self.total = total
        self.hasher = md5()
        self.size = 0
        self.chunk = 'test' * 16 * 2 ** 10

    @property
    def etag(self):
        return self.hasher.hexdigest()

    def __iter__(self):
        return self

    def next(self):
        if self.size > self.total:
            raise StopIteration()
        self.size += len(self.chunk)
        self.hasher.update(self.chunk)
        return self.chunk

    def __next__(self):
        return next(self)


class TestReconstructorRebuild(ECProbeTest):

    def setUp(self):
        super(TestReconstructorRebuild, self).setUp()
        self.container_name = 'container-%s' % uuid.uuid4()
        self.object_name = 'object-%s' % uuid.uuid4()
        # sanity
        self.assertEqual(self.policy.policy_type, EC_POLICY)
        self.reconstructor = Manager(["object-reconstructor"])

        # create EC container
        headers = {'X-Storage-Policy': self.policy.name}
        client.put_container(self.url, self.token, self.container_name,
                             headers=headers)

        # PUT object and POST some metadata
        contents = Body()
        headers = {'x-object-meta-foo': 'meta-foo'}
        self.headers_post = {'x-object-meta-bar': 'meta-bar'}

        self.etag = client.put_object(self.url, self.token,
                                      self.container_name,
                                      self.object_name,
                                      contents=contents, headers=headers)
        client.post_object(self.url, self.token, self.container_name,
                           self.object_name, headers=dict(self.headers_post))

        self.opart, self.onodes = self.object_ring.get_nodes(
            self.account, self.container_name, self.object_name)

        # stash frag etags and metadata for later comparison
        self.frag_headers, self.frag_etags = self._assert_all_nodes_have_frag()
        for node_index, hdrs in self.frag_headers.items():
            # sanity check
            self.assertIn(
                'X-Backend-Durable-Timestamp', hdrs,
                'Missing durable timestamp in %r' % self.frag_headers)

    def proxy_get(self):
        # GET object
        headers, body = client.get_object(self.url, self.token,
                                          self.container_name,
                                          self.object_name,
                                          resp_chunk_size=64 * 2 ** 10)
        resp_checksum = md5()
        for chunk in body:
            resp_checksum.update(chunk)
        return headers, resp_checksum.hexdigest()

    def direct_get(self, node, part, require_durable=True):
        req_headers = {'X-Backend-Storage-Policy-Index': int(self.policy)}
        if not require_durable:
            req_headers.update(
                {'X-Backend-Fragment-Preferences': json.dumps([])})
        headers, data = direct_client.direct_get_object(
            node, part, self.account, self.container_name,
            self.object_name, headers=req_headers,
            resp_chunk_size=64 * 2 ** 20)
        hasher = md5()
        for chunk in data:
            hasher.update(chunk)
        return headers, hasher.hexdigest()

    def _break_nodes(self, failed, non_durable):
        # delete partitions on the failed nodes and remove durable marker from
        # non-durable nodes
        for i, node in enumerate(self.onodes):
            part_dir = self.storage_dir('object', node, part=self.opart)
            if i in failed:
                shutil.rmtree(part_dir, True)
                try:
                    self.direct_get(node, self.opart)
                except direct_client.DirectClientException as err:
                    self.assertEqual(err.http_status, 404)
            elif i in non_durable:
                for dirs, subdirs, files in os.walk(part_dir):
                    for fname in files:
                        if fname.endswith('.data'):
                            non_durable_fname = fname.replace('#d', '')
                            os.rename(os.path.join(dirs, fname),
                                      os.path.join(dirs, non_durable_fname))
                            break
                headers, etag = self.direct_get(node, self.opart,
                                                require_durable=False)
                self.assertNotIn('X-Backend-Durable-Timestamp', headers)
            try:
                os.remove(os.path.join(part_dir, 'hashes.pkl'))
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise

    def _format_node(self, node):
        return '%s#%s' % (node['device'], node['index'])

    def _assert_all_nodes_have_frag(self):
        # check all frags are in place
        failures = []
        frag_etags = {}
        frag_headers = {}
        for node in self.onodes:
            try:
                headers, etag = self.direct_get(node, self.opart)
                frag_etags[node['index']] = etag
                del headers['Date']  # Date header will vary so remove it
                frag_headers[node['index']] = headers
            except direct_client.DirectClientException as err:
                failures.append((node, err))
        if failures:
            self.fail('\n'.join(['    Node %r raised %r' %
                                 (self._format_node(node), exc)
                                 for (node, exc) in failures]))
        return frag_headers, frag_etags

    @contextmanager
    def _annotate_failure_with_scenario(self, failed, non_durable):
        try:
            yield
        except (AssertionError, ClientException) as err:
            self.fail(
                'Scenario with failed nodes: %r, non-durable nodes: %r\n'
                ' failed with:\n%s' %
                ([self._format_node(self.onodes[n]) for n in failed],
                 [self._format_node(self.onodes[n]) for n in non_durable], err)
            )

    def _test_rebuild_scenario(self, failed, non_durable,
                               reconstructor_cycles):
        # helper method to test a scenario with some nodes missing their
        # fragment and some nodes having non-durable fragments
        with self._annotate_failure_with_scenario(failed, non_durable):
            self._break_nodes(failed, non_durable)

        # make sure we can still GET the object and it is correct; the
        # proxy is doing decode on remaining fragments to get the obj
        with self._annotate_failure_with_scenario(failed, non_durable):
            headers, etag = self.proxy_get()
            self.assertEqual(self.etag, etag)
            for key in self.headers_post:
                self.assertIn(key, headers)
                self.assertEqual(self.headers_post[key], headers[key])

        # fire up reconstructor
        for i in range(reconstructor_cycles):
            self.reconstructor.once()

        # check GET via proxy returns expected data and metadata
        with self._annotate_failure_with_scenario(failed, non_durable):
            headers, etag = self.proxy_get()
            self.assertEqual(self.etag, etag)
            for key in self.headers_post:
                self.assertIn(key, headers)
                self.assertEqual(self.headers_post[key], headers[key])
        # check all frags are intact, durable and have expected metadata
        with self._annotate_failure_with_scenario(failed, non_durable):
            frag_headers, frag_etags = self._assert_all_nodes_have_frag()
            self.assertEqual(self.frag_etags, frag_etags)
            # self._frag_headers include X-Backend-Durable-Timestamp so this
            # assertion confirms that the rebuilt frags are all durable
            self.assertEqual(self.frag_headers, frag_headers)

    def test_rebuild_missing_frags(self):
        # build up a list of node lists to kill data from,
        # first try a single node
        # then adjacent nodes and then nodes >1 node apart
        single_node = (random.randint(0, 5),)
        adj_nodes = (0, 5)
        far_nodes = (0, 4)

        for failed_nodes in [single_node, adj_nodes, far_nodes]:
            self._test_rebuild_scenario(failed_nodes, [], 1)

    def test_rebuild_non_durable_frags(self):
        # build up a list of node lists to make non-durable,
        # first try a single node
        # then adjacent nodes and then nodes >1 node apart
        single_node = (random.randint(0, 5),)
        adj_nodes = (0, 5)
        far_nodes = (0, 4)

        for non_durable_nodes in [single_node, adj_nodes, far_nodes]:
            self._test_rebuild_scenario([], non_durable_nodes, 1)

    def test_rebuild_with_missing_frags_and_non_durable_frags(self):
        # pick some nodes with parts deleted, some with non-durable fragments
        scenarios = [
            # failed, non-durable
            ((0, 2), (4,)),
            ((0, 4), (2,)),
        ]
        for failed, non_durable in scenarios:
            self._test_rebuild_scenario(failed, non_durable, 3)
        scenarios = [
            # failed, non-durable
            ((0, 1), (2,)),
            ((0, 2), (1,)),
        ]
        for failed, non_durable in scenarios:
            # why 2 repeats? consider missing fragment on nodes 0, 1  and
            # missing durable on node 2: first reconstructor cycle on node 3
            # will make node 2 durable, first cycle on node 5 will rebuild on
            # node 0; second cycle on node 0 or 2 will rebuild on node 1. Note
            # that it is possible, that reconstructor processes on each node
            # run in order such that all rebuild complete in once cycle, but
            # that is not guaranteed, we allow 2 cycles to be sure.
            self._test_rebuild_scenario(failed, non_durable, 2)
        scenarios = [
            # failed, non-durable
            ((0, 2), (1, 3, 5)),
            ((0,), (1, 2, 4, 5)),
        ]
        for failed, non_durable in scenarios:
            # why 3 repeats? consider missing fragment on node 0 and single
            # durable on node 3: first reconstructor cycle on node 3 will make
            # nodes 2 and 4 durable, second cycle on nodes 2 and 4 will make
            # node 1 and 5 durable, third cycle on nodes 1 or 5 will
            # reconstruct the missing fragment on node 0.
            self._test_rebuild_scenario(failed, non_durable, 3)

    def test_rebuild_partner_down(self):
        # find a primary server that only has one of it's devices in the
        # primary node list
        group_nodes_by_config = defaultdict(list)
        for n in self.onodes:
            group_nodes_by_config[self.config_number(n)].append(n)
        for config_number, node_list in group_nodes_by_config.items():
            if len(node_list) == 1:
                break
        else:
            self.fail('ring balancing did not use all available nodes')
        primary_node = node_list[0]

        # pick one it's partners to fail randomly
        partner_node = random.choice(_get_partners(
            primary_node['index'], self.onodes))

        # 507 the partner device
        device_path = self.device_dir('object', partner_node)
        self.kill_drive(device_path)

        # select another primary sync_to node to fail
        failed_primary = [n for n in self.onodes if n['id'] not in
                          (primary_node['id'], partner_node['id'])][0]
        # ... capture it's fragment etag
        failed_primary_meta, failed_primary_etag = self.direct_get(
            failed_primary, self.opart)
        # ... and delete it
        part_dir = self.storage_dir('object', failed_primary, part=self.opart)
        shutil.rmtree(part_dir, True)

        # reconstruct from the primary, while one of it's partners is 507'd
        self.reconstructor.once(number=self.config_number(primary_node))

        # the other failed primary will get it's fragment rebuilt instead
        failed_primary_meta_new, failed_primary_etag_new = self.direct_get(
            failed_primary, self.opart)
        del failed_primary_meta['Date']
        del failed_primary_meta_new['Date']
        self.assertEqual(failed_primary_etag, failed_primary_etag_new)
        self.assertEqual(failed_primary_meta, failed_primary_meta_new)

        # just to be nice
        self.revive_drive(device_path)


if __name__ == "__main__":
    unittest.main()
