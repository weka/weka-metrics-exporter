
#
# wekaapi module
#
# Python implementation of the Weka JSON RPC API.
#
# Author: Vince Fleming, vince@weka.io
#

from __future__ import division, absolute_import, print_function, unicode_literals
try:
    from future_builtins import *
except ImportError:
    pass

import sys
import os
import json
import time
import uuid
import socket
import ssl
from threading import Lock

try:
    import http.client
    httpclient = http.client
except ImportError:
    import httplib
    httpclient = httplib

try:
    from urllib.parse import urlencode, urljoin, urlparse
except ImportError:
    from urlparse import urljoin, urlparse

from logging import debug, info, warning, error, critical, getLogger

log = getLogger(__name__)
 
class HttpException(Exception):
    def __init__(self, error_code, error_msg):
        self.error_code = error_code
        self.error_msg = error_msg

"""
class JsonRpcException(Exception):
    def __init__(self, json_error):
        self.orig_json = json_error
        self.code = json_error['code']
        self.message = json_error['message']
        self.data = json_error.get('data', None)
"""


class WekaApiException(Exception):
    def __init__(self, message):
        self.message = message

class WekaApi():
    def __init__(self, host, scheme='https', port=14000, path='/api/v1', timeout=10, token_file='~/.weka/auth-token.json'):

        self._lock = Lock() # make it re-entrant (thread-safe)
        self._scheme = scheme
        self._host = host
        self._port = port
        self._path = path
        self._timeout = timeout
        self._tokens=self._get_tokens(token_file)
        self.headers = {}
        if self._tokens != None:
            self.authorization = '{0} {1}'.format(self._tokens['token_type'], self._tokens['access_token'])
            self.headers["Authorization"] = self.authorization
        else:
            self.authorization = None

        self.headers['UTC-Offset-Seconds'] = time.altzone if time.localtime().tm_isdst else time.timezone
        self.headers['CLI'] = False
        self.headers['Client-Type'] = 'WEKA'

        self._open_connection()
        log.debug( "WekaApi: connected to {}".format(self._host) )

    @staticmethod
    def format_request(message_id, method, params):
        return dict(jsonrpc='2.0',
                    method=method,
                    params=params,
                    id=message_id)

    @staticmethod
    def unique_id(alphabet='0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'):
        number = uuid.uuid4().int
        result = ''
        while number != 0:
            number, i = divmod(number, len(alphabet))
            result = alphabet[i] + result
        return result


    @staticmethod
    def _parse_url(url, default_port=14000, default_path='/api/v1'):
        parsed_url = urlparse(url)
        scheme = parsed_url.scheme if parsed_url.scheme else "https"
        if scheme not in ['http', 'https']:
            scheme = "https"
        m = re.match('^(?:(?:http|https)://)?(.+?)(?::(\d+))?(/.*)?$', str(url), re.I)
        assert m
        return scheme, m.group(1), m.group(2) or default_port, m.group(3) or default_path

    def _open_connection( self ):
        host_unreachable=False

        if self._scheme == "https":
            self._conn = httpclient.HTTPSConnection(host=self._host, port=self._port, timeout=self._timeout)
        else:
            self._conn = httpclient.HTTPConnection(host=self._host, port=self._port, timeout=self._timeout)

        try:
            self.weka_api_command( "getServerInfo" ) # test the connection
        except ssl.SSLError:
            # https failed, try http - http would never produce an ssl error
            self._scheme = "http"
            self._open_connection()

        except socket.gaierror: # general failure
            host_unreachable=True

        if host_unreachable:
            raise WekaApiException( "unable to communicate with host " + self._host )


    def _get_tokens(self, token_file):
        if token_file != None:
            tokens=None
            path = os.path.expanduser(token_file)
            if os.path.exists(path):
                try:
                    with open( path ) as fp:
                        tokens = json.load( fp )
                    return tokens
                except Exception as error:
                    log.critical( "wekaapi: unable to open token file {}".format(token_file) )
                    raise WekaApiException('warning: Could not parse {0}, ignoring file'.format(path), file=sys.stderr)
            else:
                log.error( "wekaapi: token file {} not found".format(token_file) )
        return None



    # reformat data returned so it's like the weka command's -J output
    @staticmethod
    def _format_response( method, response_obj ):
        raw_resp = response_obj["result"] 
        if method == "status":
            return( raw_resp )

        resp_list = []

        if method == "stats_show":
            for key, value_dict in raw_resp.items():
                stat_dict = {}
                stat_dict["node"] = key
                stat_dict["category"] = list(value_dict.keys())[0]
                cat_dict = value_dict[stat_dict["category"]][0]
                stats = cat_dict["stats"]
                stat_dict["stat_type"] = list(stats.keys())[0]
                tmp = stats[stat_dict["stat_type"]]
                if tmp == "null":
                    stat_dict["stat_value"] = 0
                else:
                    stat_dict["stat_value"] = tmp
                stat_dict["timestamp"] = cat_dict["timestamp"]
                resp_list.append( stat_dict )
            return resp_list

        splitmethod = method.split("_")
        words = len( splitmethod )

        # does it end in "_list"?
        if splitmethod[words-1] == "list" or method == "filesystems_get_capacity":
            for key, value_dict in raw_resp.items():
                newkey = key.split( "I" )[0].lower() + "_id"
                value_dict[newkey] = key
                resp_list.append( value_dict )
            return resp_list

        # ignore other method types for now.
        return raw_resp
    # end of _format_response()

    # log into the api
    def _login( self ):
        login_message_id = self.unique_id()

        if self.authorization != None:
            # try to refresh the auth since we have prior authorization
            params = dict(refresh_token=self._tokens["refresh_token"])
            login_request = self.format_request(login_message_id, "user_refresh_token", params)
        else:
            # try default login info - we have no tokens.
            params = dict(username="admin", password="admin")
            login_request = self.format_request(login_message_id, "user_login", params)

        log.debug( "logging into host {}".format(self._host) )

        self._conn.request('POST', self._path, json.dumps(login_request), self.headers) # POST a user_login request
        response = self._conn.getresponse()
        response_body = response.read().decode('utf-8')

        if response.status != 200:  # default login creds/refresh failed
            raise HttpException(response.status, response_body)

        response_object = json.loads(response_body)
        self._tokens = response_object["result"]
        if self._tokens != {}:
            self.authorization = '{0} {1}'.format(self._tokens['token_type'], self._tokens['access_token'])
        else:
            self.authorization = None
            self._tokens= None
            log.critical( "Login failed" )
            raise WekaApiException( "Login failed" )

        self.headers["Authorization"] = self.authorization
        log.debug( "login to {} successful".format(self._host) )

        # end of _login()

    def weka_api_command(self, *args, **kwargs):
        with self._lock:        # make it thread-safe - just in case someone tries to do 2 commands at the same time
            message_id = self.unique_id()
            method = args[0]
            request = self.format_request(message_id, args[0], kwargs)
            log.debug( "wekaapi: submitting command {} {}".format(method, kwargs) )

            for i in range(3):  # give it 3 tries
                #log.debug( "Making POST request to {}: {} {} {}".format(self._host, self._path, json.dumps(request), str(self.headers)) )
                self._conn.request('POST', self._path, json.dumps(request), self.headers)
                try:
                    response = self._conn.getresponse()
                except http.client.RemoteDisconnected as e:
                    log.debug( "client disconnected, attempting re-open to {}".format(self._host) )
                    self._open_connection() # re-open connection
                    continue

                response_body = response.read().decode('utf-8')

                if response.status == httpclient.UNAUTHORIZED:      # not logged in?
                    self._login()
                    continue    # go back to the top of the loop and try again now that we're logged in

                if response.status in (httpclient.OK, httpclient.CREATED, httpclient.ACCEPTED):
                    response_object = json.loads(response_body)
                    if 'error' in response_object:
                        raise WekaApiException(response_object['error'])
                    self._conn.close()
                    log.debug( "good response from {}".format(self._host) )
                    return self._format_response( method, response_object )

                if response.status == httpclient.MOVED_PERMANENTLY:
                    oldhost = self._host
                    self._scheme, self._host, self._port, self._path = self._parse_url(response.getheader('Location'))
                    log.debug( "redirection: {} moved to {}".format(oldhost, self._host) )
                    self._open_connection()
                else:
                    raise HttpException(response.status, response.reason)

            log.debug( "other exception occurred on host {}".format(self._host) )
            raise HttpException(response.status, response_body)



# main is for testing
def main():

    api_connection = WekaApi( '172.20.0.128' )

    #parms={u'category': u'ops', u'stat': u'OPS', u'interval': u'1m', u'per_node': True}
    #print( parms)
    #print( json.dumps( api_connection.weka_api_command( "status" ) , indent=4, sort_keys=True))
    #print( json.dumps( api_connection.weka_api_command( "hosts_list" ) , indent=4, sort_keys=True))
    print( json.dumps( api_connection.weka_api_command( "nodes_list" ) , indent=4, sort_keys=True))
    #print( json.dumps( api_connection.weka_api_command( "filesystems_list" ) , indent=4, sort_keys=True))
    #print( json.dumps( api_connection.weka_api_command( "filesystems_get_capacity" ) , indent=4, sort_keys=True))

    # more examples:
    #print( json.dumps(api_connection.weka_api_command( "stats_show", category='ops', stat='OPS', interval='1m', per_node=True ), indent=4, sort_keys=True) )
    #print( api_connection.weka_api_command( "status", fred="mary" ) )   # should produce error; for checking errorhandling



    # ======================================================================


if __name__ == "__main__":
    sys.exit(main())

