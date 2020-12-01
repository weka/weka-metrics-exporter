
from logging import debug, info, warning, error, critical, getLogger, DEBUG, StreamHandler
import logging

from wekaapi import WekaApi
from reserve import reservation_list
import traceback
from threading import Lock

log = getLogger(__name__)

class WekaHost(object):
    def __init__( self, hostname, tokenfile=None ):
        self.name = hostname
        self.apitoken = tokenfile
        self.api_obj = None
        self._lock = Lock()
        # do we need a backpointer to the cluster?

        try:
            if self.apitoken != None:
                self.api_obj = WekaApi(self.name, token_file=self.apitoken, timeout=10)
            else:
                self.api_obj = WekaApi(self.name, timeout=10)
        except Exception as exc:
            log.error(f"{exc}")

        if self.api_obj == None:
            # can't open API session, fail.
            #log.error("WekaHost: unable to open API session")
            raise Exception("Unable to open API session")

    def call_api( self, method=None, parms={} ):
        with self._lock:    # let's force only one command at a time
            log.debug( "calling Weka API on host {}".format(self.name) )
            return self.api_obj.weka_api_command( method, parms )

    def __str__(self):
        return self.name

    def __repr__(self):
        return self.name

# per-cluster object 
class WekaCluster(object):

    # collects on a per-cluster basis
    # hostname format is "host1,host2,host3,host4" (can be ip addrs, even only one of the cluster hosts)
    # auth file is output from "weka user login" command.
    # autohost is a bool = should we distribute the api call load among all weka servers
    def __init__( self, hostname, authfile=None, autohost=True ):
        # object instance global data
        self.errors = 0
        self.clustersize = 0
        self.loadbalance = True
        self.orig_hostlist = None
        self.name = ""

        self.orig_hostlist = hostname.split(",")  # takes a comma-separated list of hosts
        self.hosts = reservation_list()
        self.authfile = authfile
        self.loadbalance = autohost

        """
        # create objects for the hosts
        for hostname in self.orig_hostlist:
            try:
                hostobj = WekaHost(hostname, self.authfile)
            except:
                pass
            else:
                self.hosts.add(hostobj)

        # get the rest of the cluster
        api_return = self.call_api( method="hosts_list", parms={} )
        for host in api_return:
            hostname = host["hostname"]
            if host["mode"] == "backend":
                self.clustersize += 1
                if host["state"] == "ACTIVE" and host["status"] == "UP":
                    if not hostname in self.hosts:
                        try:
                            hostobj = WekaHost(hostname, self.authfile)
                        except:
                            pass
                        else:
                            self.hosts.add(hostobj)
        """
        self.refresh_config()

        # get the cluster name via a manual api call    (failure raises and fails creation of obj)
        api_return = self.call_api( method="status", parms={} )
        self.name = api_return['name']
        self.GUID = api_return['guid']

        #log.debug( self.hosts )
        #log.debug( "wekaCluster {} created. Cluster has {} members, {} are online".format(self.name, self.clustersize, len(self.hosts)) )
        # ------------- end of __init__() -------------

    def refresh_config(self):
        if len(self.hosts) == 0:
            # create objects for the hosts; recover from total failure
            for hostname in self.orig_hostlist:
                try:
                    hostobj = WekaHost(hostname, self.authfile)
                except:
                    pass
                else:
                    self.hosts.add(hostobj)
        # get the rest of the cluster (bring back any that had previously disappeared, or have been added)
        self.clustersize = 0
        api_return = self.call_api( method="hosts_list", parms={} )
        for host in api_return:
            hostname = host["hostname"]
            if host["mode"] == "backend":
                self.clustersize += 1
                if host["state"] == "ACTIVE" and host["status"] == "UP":
                    if not hostname in self.hosts:
                        try:
                            hostobj = WekaHost(hostname, self.authfile)
                        except:
                            pass
                        else:
                            self.hosts.add(hostobj)
        log.debug( "wekaCluster {} refreshed. Cluster has {} members, {} are online".format(self.name, self.clustersize, len(self.hosts)) )

    def get_guid(self):
        return self.GUID

    def __str__(self):
        return str(self.name)

    def sizeof(self):
        return len(self.hosts)

    # cluster-level call_api() will retry commands on another host in the cluster on failure
    def call_api( self, method=None, parms={} ):
        host = self.hosts.reserve()
        api_return = None
        while host != None:
            log.debug( "calling Weka API on cluster {}, host {}".format(self.name,host) )
            try:
                api_return = host.call_api( method, parms )
            except Exception as exc:
                # something went wrong...  stop talking to this host from here on.  We'll try to re-establish communications later
                last_hostname = host.name
                self.hosts.remove(host)     # remove it from the hostlist iterable
                host = self.hosts.reserve() # try another host; will return None if none left or failure
                self.errors += 1
                log.error( "cluster={}, error {} spawning command {} on host {}. Retrying on {}.".format(
                        self.name, exc, str(method), last_hostname, str(host)) )
                print(traceback.format_exc())
                continue

            self.hosts.release(host)    # release it so it can be used for more queries
            return api_return

        # ran out of hosts to talk to!
        raise Exception("unable to communicate with cluster")

        # ------------- end of call_api() -------------

if __name__ == "__main__":
    logger = getLogger()
    logger.setLevel(DEBUG)
    log.setLevel(DEBUG)
    FORMAT = "%(filename)s:%(lineno)s:%(funcName)s():%(message)s"
    # create handler to log to stderr
    console_handler = StreamHandler()
    console_handler.setFormatter(logging.Formatter(FORMAT))
    logger.addHandler(console_handler)

    print( "creating cluster" )
    cluster = WekaCluster( "172.20.0.128,172.20.0.129,172.20.0.135" )

    print( "cluster created" )

    print(cluster)

    print( cluster.call_api( method="status", parms={} ) )



















