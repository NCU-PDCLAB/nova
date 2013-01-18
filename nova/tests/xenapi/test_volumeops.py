# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (c) 2013 Citrix Systems, Inc.
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


import mock
from nova.tests.xenapi import stubs
from nova.virt.xenapi import volumeops


class VolumeDriverRegistryTestCase(stubs.XenAPITestBase):
    def _get_volumeops(self):
        self.flags(
            xenapi_volume_drivers=[
                'config1', 'config2']
        )
        return volumeops.VolumeOps(stubs.FakeSessionForVMTests(None))

    def test_volume_driver_registry_populated(self):
        with mock.patch('nova.virt.driver.driver_dict_from_config',
                        return_value='REGISTRY') as driver_dict_from_config:
            volops = self._get_volumeops()

        driver_dict_from_config.assert_called_once_with(
            ['config1', 'config2'], session=volops._session,
            volumeops=volops
        )

        self.assertEquals(
            volops._volume_driver_registry, 'REGISTRY')
