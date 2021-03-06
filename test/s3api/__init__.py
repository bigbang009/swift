# Copyright (c) 2019 SwiftStack, Inc.
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

import logging
import os
import unittest

import boto3
from six.moves import urllib

from swift.common.utils import config_true_value

from test import get_config

_CONFIG = None


# boto's loggign can get pretty noisy; require opt-in to see it all
if not config_true_value(os.environ.get('BOTO3_DEBUG')):
    logging.getLogger('boto3').setLevel(logging.INFO)
    logging.getLogger('botocore').setLevel(logging.INFO)


class ConfigError(Exception):
    '''Error test conf misconfigurations'''


def get_opt_or_error(option):
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = get_config('s3api_test')

    value = _CONFIG.get(option)
    if not value:
        raise ConfigError('must supply [s3api_test]%s' % option)
    return value


def get_opt(option, default=None):
    try:
        return get_opt_or_error(option)
    except ConfigError:
        return default


def get_s3_client(user=1, signature_version='s3v4', addressing_style='path'):
    '''
    Get a boto3 client to talk to an S3 endpoint.

    :param user: user number to use. Should be one of:
        1 -- primary user
        2 -- secondary user
        3 -- unprivileged user
    :param signature_version: S3 signing method. Should be one of:
        s3 -- v2 signatures; produces Authorization headers like
              ``AWS access_key:signature``
        s3-query -- v2 pre-signed URLs; produces query strings like
                    ``?AWSAccessKeyId=access_key&Signature=signature``
        s3v4 -- v4 signatures; produces Authorization headers like
                ``AWS4-HMAC-SHA256
                Credential=access_key/date/region/s3/aws4_request,
                Signature=signature``
        s3v4-query -- v4 pre-signed URLs; produces query strings like
                      ``?X-Amz-Algorithm=AWS4-HMAC-SHA256&
                      X-Amz-Credential=access_key/date/region/s3/aws4_request&
                      X-Amz-Signature=signature``
    :param addressing_style: One of:
        path -- produces URLs like ``http(s)://host.domain/bucket/key``
        virtual -- produces URLs like ``http(s)://bucket.host.domain/key``
    '''
    endpoint = get_opt_or_error('endpoint')
    scheme = urllib.parse.urlsplit(endpoint).scheme
    if scheme not in ('http', 'https'):
        raise ConfigError('unexpected scheme in endpoint: %r; '
                          'expected http or https' % scheme)
    region = get_opt('region', 'us-east-1')
    access_key = get_opt_or_error('access_key%d' % user)
    secret_key = get_opt_or_error('secret_key%d' % user)

    ca_cert = get_opt('ca_cert')
    if ca_cert is not None:
        try:
            # do a quick check now; it's more expensive to have boto check
            os.stat(ca_cert)
        except OSError as e:
            raise ConfigError(str(e))

    return boto3.client(
        's3',
        endpoint_url=endpoint,
        region_name=region,
        use_ssl=(scheme == 'https'),
        verify=ca_cert,
        config=boto3.session.Config(s3={
            'signature_version': signature_version,
            'addressing_style': addressing_style,
        }),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


class BaseS3TestCase(unittest.TestCase):
    # Default to v4 signatures (as aws-cli does), but subclasses can override
    signature_version = 's3v4'

    @classmethod
    def get_s3_client(cls, user):
        return get_s3_client(user, cls.signature_version)

    @classmethod
    def clear_bucket(cls, client, bucket):
        for key in client.list_objects(Bucket=bucket).get('Contents', []):
            client.delete_key(Bucket=bucket, Key=key['Name'])

    @classmethod
    def clear_account(cls, client):
        for bucket in client.list_buckets()['Buckets']:
            cls.clear_bucket(client, bucket['Name'])
            client.delete_bucket(Bucket=bucket['Name'])

    def tearDown(self):
        client = self.get_s3_client(1)
        self.clear_account(client)
        try:
            client = self.get_s3_client(2)
        except ConfigError:
            pass
        else:
            self.clear_account(client)
