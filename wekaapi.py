#!/usr/bin/env python3

#
# WekaApi module
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

class HttpException(Exception):
    def __init__(self, error_code, error_msg):
        self.error_code = error_code
        self.error_msg = error_msg


class JsonRpcException(Exception):
    def __init__(self, json_error):
        self.orig_json = json_error
        self.code = json_error['code']
        self.message = json_error['message']
        self.data = json_error.get('data', None)


class WekaApiException(Exception):
    def __init__(self, message):
        self.message = message

class WekaApi():
    def __init__(self, host, scheme='https', port=14000, path='/api/v1', timeout=10, token_file='~/.weka/auth-token.json'):

        self._lock = Lock() # make it re-entrant (thread-safe)
        self.scheme = scheme
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
        print( "WekaApi: connected to {}".format(host) )

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


    def _open_connection( self ):
        host_unreachable=False
        self._conn = httpclient.HTTPSConnection(host=self._host, port=self._port, timeout=self._timeout)
        try:
            self.weka_api_command( "getServerInfo" ) # test the connection
        except ssl.SSLError:
            #print( "https failed, using http" )
            self._conn = httpclient.HTTPConnection(host=self._host, port=self._port, timeout=self._timeout)
            try:
                self.weka_api_command( "getServerInfo" ) # test the connection
            except socket.gaierror as exc:
                print(f"EXCEPTION {exc}")
                print( "unable to communicate2 with host " + self._host )
                host_unreachable=True
        except socket.gaierror as e:
            print( "unable to communicate with host " + self._host )
            host_unreachable=True

        if host_unreachable:
            raise WekaApiException( "unable to communicate with host " + self._host )


    def _get_tokens(self, token_file):
        if token_file != None:
            self._tokens=None
            path = os.path.expanduser(token_file)
            if os.path.exists(path):
                try:
                    with open( path ) as fp:
                        self._tokens = json.load( fp )
                    return
                except Exception as error:
                    print('warning: Could not parse {0}, ignoring file'.format(path), file=sys.stderr)

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


    def weka_api_command(self, *args, **kwargs):
        with self._lock:        # make it thread-safe - just in case someone tries to do 2 commands at the same time
            #print( "in weka_api_command()" )
            #print( "args=" + str(args) )
            #print( "kwargs=" + str(kwargs) )
            message_id = self.unique_id()
            method = args[0]
            request = self.format_request(message_id, args[0], kwargs)

            for i in range(3):
                #print( "Making POST request: " + self._path + " " + json.dumps(request) + " " + str(self.headers) )
                self._conn.request('POST', self._path, json.dumps(request), self.headers)
                try:
                    response = self._conn.getresponse()
                except http.client.RemoteDisconnected as e:
                    print( e )
                    self._open_connection() # re-open connection
                    continue
                response_body = response.read().decode('utf-8')

                if response.status == httpclient.UNAUTHORIZED:
                    login_message_id = self.unique_id()

                    if self.authorization != None:
                        # try to refresh the auth since we have prior authorization
                        #print( "trying refresh token" )
                        params = dict(refresh_token=self._tokens["refresh_token"])
                        login_request = self.format_request(login_message_id, "user_refresh_token", params)
                    else:
                        # try default login info - we have no tokens.
                        #print( "trying default login info" )
                        params = dict(username="admin", password="admin")
                        login_request = self.format_request(login_message_id, "user_login", params)

                    self._conn.request('POST', self._path, json.dumps(login_request), self.headers) # POST a user_login request
                    response = self._conn.getresponse()
                    response_body = response.read().decode('utf-8')

                    if response.status != 200:  # default login creds/refresh failed
                        raise HttpException(response.status, response_body)

                    response_object = json.loads(response_body)
                    self._tokens = response_object["result"]
                    #print( "self._tokens = " + str(self._tokens) )
                    if self._tokens != {}:
                        self.authorization = '{0} {1}'.format(self._tokens['token_type'], self._tokens['access_token'])
                    else:
                        self.authorization = None
                        self._tokens= None
                        print( "login failed?" )    # should raise error
                        # login failed?
                    self.headers["Authorization"] = self.authorization
                    continue
                if response.status in (httpclient.OK, httpclient.CREATED, httpclient.ACCEPTED):
                    response_object = json.loads(response_body)
                    if 'error' in response_object:
                        raise JsonRpcException(response_object['error'])
                    #print( type(response_object['result']) )
                    #return response_object['result']
                    return self._format_response( method, response_object )
                # figure out how to handle this later - vince
                #if response.status == httpclient.MOVED_PERMANENTLY:
                #    scheme, host, port, self._path = parse_url(response.getheader('Location'))
                #    self._conn = httpclient.HTTPConnection(host=host, port=port, timeout=self._conn.timeout) if scheme=='http' else httpclient.HTTPSConnection(host=host, port=port, timeout=self._conn.timeout)
                #else:
                #    raise HttpException(response.status, response.reason)

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


