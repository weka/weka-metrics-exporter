
#
# collector module - implement prometheus_client collectors
#

# author: Vince Fleming, vince@weka.io


from prometheus_client import Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily, InfoMetricFamily, GaugeHistogramMetricFamily
import time, datetime
import yaml
from threading import Lock
import traceback
import logging
import logging.handlers
from logging import debug, info, warning, error, critical, getLogger, DEBUG
#from itertools import cycle

# local imports
from wekaapi import WekaApi
from sthreads import simul_threads
from reserve import reservation_list

class WekaIOHistogram(Histogram):
    def multi_observe( self, iosize, value ):
        """Observe the given amount."""
        self._sum.inc(iosize * value)
        for i, bound in enumerate(self._upper_bounds):
            if float(iosize) <= bound:
                self._buckets[i].inc(value)
                break

# ---------------- end of WekaIOHistogram definition ------------

#
# Module Globals
#
# instrument thyself
gather_gauge = Gauge('weka_metrics_exporter_weka_metrics_gather_seconds', 'Time spent gathering cluster info')
weka_collect_gauge = Gauge('weka_collect_seconds', 'Time spent in Prometheus collect')
cmd_exec_gauge = Gauge('weka_metrics_exporter_cmd_execute_seconds', 'Time spent gathering statistics', ["stat","cluster"])

# initialize logger - configured in main routine
log = getLogger(__name__)

# wekaCollector_objlist[clustername].wekadata    should be globally accessible?

# our prometheus collector
class wekaCollector(object):
    CLUSTERSTATS = {
        'weka_overview_activity_ops': ['Weka IO Summary number of operations', ['cluster'], 'num_ops'],
        'weka_overview_activity_read_iops': ['Weka IO Summary number of read operations', ['cluster'], 'num_reads'],
        'weka_overview_activity_read_bytespersec': ['Weka IO Summary read rate', ['cluster'], 'sum_bytes_read'],
        'weka_overview_activity_write_iops': ['Weka IO Summary number of write operations', ['cluster'], 'num_writes'],
        'weka_overview_activity_write_bytespersec': ['Weka IO Summary write rate', ['cluster'], 'sum_bytes_written'],
        'weka_overview_activity_object_download_bytespersec': ['Weka IO Summary Object Download BPS', ['cluster'], 'obs_download_bytes_per_second'],
        'weka_overview_activity_object_upload_bytespersec': ['Weka IO Summary Object Upload BPS', ['cluster'], 'obs_upload_bytes_per_second']
        }

    INFOSTATS = {
        'weka_host_spares': ['Weka cluster # of hot spares', ["cluster"]],
        'weka_host_spares_bytes': ['Weka capacity of hot spares', ["cluster"]],
        'weka_drive_storage_total_bytes': ['Weka total drive capacity', ["cluster"]],
        'weka_drive_storage_unprovisioned_bytes': ['Weka unprovisioned drive capacity', ["cluster"]],
        'weka_num_servers_active': ['Number of active weka servers', ["cluster"]],
        'weka_num_servers_total': ['Total number of weka servers', ["cluster"]],
        'weka_num_clients_active': ['Number of active weka clients', ["cluster"]],
        'weka_num_clients_total': ['Total number of weka clients', ["cluster"]],
        'weka_num_drives_active': ['Number of active weka drives', ["cluster"]],
        'weka_num_drives_total': ['Total number of weka drives', ["cluster"]]
        }
    wekaIOCommands = {}
    weka_stat_list = {} # category: {{ stat:unit}, {stat:unit}}

    def __init__( self, configfile ):

        # dynamic module globals
        # this comes from a yaml file
        buckets = []    # same for everyone
        self._access_lock = Lock()
        self.gather_timestamp = None
        self.collect_time = None

        self.wekaCollector_objlist = {}

        global weka_stat_list 
        weka_stat_list = self._load_config( configfile )

        # set up commands to get stats defined in config file
        # category: {{ stat:unit}, {stat:unit}}
        for category, stat_dict in weka_stat_list.items():
            for stat, unit in stat_dict.items():
                # have to create the category keys, so do it with a try: block
                if category not in self.wekaIOCommands:
                    self.wekaIOCommands[category] = {}

                parms = dict( category=category, stat=stat, interval='1m', per_node=True, no_zeroes=True )
                self.wekaIOCommands[category][stat] = dict( method="stats_show", parms=parms )

        # vince - make this a module global, as it applies to all objects of this type
        # set up buckets, [4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576, 2097152, 4194304, 8388608, 16777216, 33554432, 67108864, 134217728, inf]
        for i in range(12,12+16):
            buckets.append( 2 ** i )

        buckets.append( float("inf") )

        log.debug( "wekaCollector created" )

    def get_commandlist( self ):
        return self.wekaIOCommands

    def get_weka_stat_list( self ):
        return weka_stat_list

    def add_cluster( self, cluster_obj ):
        self.wekaCollector_objlist[cluster_obj.clustername] = cluster_obj

    # load the config file
    @staticmethod
    def _load_config( inputfile ):
        with open( inputfile ) as f:
            try:
                return yaml.load(f, Loader=yaml.FullLoader)
            except AttributeError:
                return yaml.load(f)

    # module global metrics allows for getting data from multiple clusters in multiple threads - DO THIS WITH A LOCK
    def _reset_metrics( self ):
        # create all the metrics objects that we'll fill in elsewhere
        global metric_objs
        metric_objs={}
        metric_objs['wekainfo'] = InfoMetricFamily( 'weka', "Information about the Weka cluster" )
        metric_objs['wekauptime'] = GaugeMetricFamily( 'weka_uptime', "Weka cluster uptime", labels=["cluster"] )
        for name, parms in self.CLUSTERSTATS.items():
            metric_objs["cluster_stat_"+name] = GaugeMetricFamily( name, parms[0], labels=parms[1] )
        for name, parms in self.INFOSTATS.items():
            metric_objs[name] = GaugeMetricFamily( name, parms[0], labels=parms[1] )
        metric_objs['weka_protection'] = GaugeMetricFamily( 'weka_protection', 'Weka Data Protection Status', labels=["cluster",'numFailures'] )
        metric_objs['weka_fs_utilization_percent'] = GaugeMetricFamily( 'weka_fs_utilization_percent', 'Filesystem % used', labels=["cluster",'fsname'] )
        metric_objs['weka_fs_size_bytes'] = GaugeMetricFamily( 'weka_fs_size_bytes', 'Filesystem size', labels=["cluster",'name'] )
        metric_objs['weka_fs_used_bytes'] = GaugeMetricFamily( 'weka_fs_used_bytes', 'Filesystem used capacity', labels=["cluster",'name'] )
        metric_objs['weka_stats_gauge'] = GaugeMetricFamily('weka_stats', 'WekaFS statistics. For more info refer to: https://docs.weka.io/usage/statistics/list-of-statistics', 
                labels=['cluster','host_name','host_role','node_id','node_role','category','stat','unit'])
        metric_objs['weka_io_histogram'] = GaugeHistogramMetricFamily( "weka_blocksize", "weka blocksize distribution histogram", 
                labels=['cluster','host_name','host_role','node_id','node_role','category','stat','unit'] )


    @weka_collect_gauge.time()
    def collect( self ):
        with self._access_lock:     # be thread-safe
            should_gather = False
            now = time.time()
            secs_since_last_min = now % 60
            secs_to_next_min = 60 - secs_since_last_min
            log.info( "secs_since_last_min {}, secs_to_next_min {}".format( int(secs_since_last_min), int(secs_to_next_min) ))

            if self.gather_timestamp == None:
                log.debug( "never gathered before" )
                self.gather_timestamp = now     # we've not collected before
                should_gather = True
                self.collect_time = now - now
            
            # has a collection been done in this minute? (weka updates at the top of the minute)
            secs_since_last_gather = now - self.gather_timestamp
            log.info( "secs_since_last_gather {}".format( int(secs_since_last_gather) ))
            # has it been more than a min, or have we not gathered since the top of the minute?
            if secs_since_last_gather > 60 or secs_since_last_gather > secs_since_last_min:
                should_gather = True
                log.debug( "more than a minute since last gather or in new min" )

            if secs_to_next_min < 5 and should_gather:
                log.debug( "sleeping {} seconds".format( int(secs_to_next_min +1) ) )
                time.sleep( secs_to_next_min +1 )   # take us past the top of the minute so we get fresh stats
                now = time.time()                   # update now because we slept

            if should_gather:
                log.info( "gathering" )
                self.gather_timestamp = now
                self._reset_metrics()
                thread_runner = simul_threads( len( self.wekaCollector_objlist) )   # one thread per cluster
                for clustername, cluster in self.wekaCollector_objlist.items():
                    thread_runner.new( cluster.gather )
                thread_runner.run()

            # ok, the prometheus_client module calls this method TWICE every time it scrapes...  ugh
            last_collect = self.collect_time
            self.collect_time = time.time()     # reset to now

            log.debug( "secs since last collect = {}, should_gather = {}".format( int(self.collect_time - last_collect), should_gather ) )

            # prom should always be like 60 secs; the double-call is one after the next
            if int(self.collect_time - last_collect) > 0:
                log.info( "returning stats" )     # only announce once

            # yield for each metric 
            for metric in metric_objs.values():
                yield metric

# per-cluster object - subclass of wekaCollector()
class wekaCluster(wekaCollector):
    WEKAINFO = {
        "hostList": dict( method="hosts_list", parms={} ),
        "clusterinfo": dict( method="status", parms={} ),
        "nodeList": dict( method="nodes_list", parms={} ),
        "fs_stat": dict( method="filesystems_get_capacity", parms={} )
        }

    # collects on a per-cluster basis
    def __init__( self, hostname, authfile, autohost ):
        # object instance global data
        self.collected_data = {}
        self.histograms = {}
        self.loadbalance = True
        self.orig_hostlist = None
        self.current_hostlist = None
        self.host_iter = None
        self.api_objs={}
        self.wekadata = {}
        self.authfile=None
        self.weka_maps = { "node-host": {}, "node-role": {}, "host-role": {} }
        self.clustername = ""
        self.threaderror = False    # indicates that a thread had a terminal error

        self.orig_hostlist = hostname.split(",")  # takes a comma-separated list of hosts
        self.current_hostlist = list(self.orig_hostlist)  # copy it
        self.host_iter = reservation_list()
        self.hosts = reservation_list()
        self.authfile = authfile
        self.loadbalance = autohost


        # create objects for the hosts
        for hostname in self.orig_hostlist:
            try:
                hostobj = WekaHost(hostname, self.authfile)
            except:
                pass
            else:
                self.hosts.add(hostobj)

        # get the cluster name via a manual api call    (failure raises and fails creation of obj)
        self._call_weka_api( "clusterinfo", self.WEKAINFO["clusterinfo"], None )
        self.clustername = self.wekadata["clusterinfo"]['name']

        log.debug( "wekaCluster {} created".format(self.clustername) )

        # ------------- end of __init__() -------------

    @staticmethod
    def _parse_sizes_values( value_string ):  # returns list of tuples of [(iosize,value),(iosize,value),...], and their sum
        # example input: "[32768..65536] 19486, [65536..131072] 1.57837e+06"
        gsum = 0
        stat_list=[]
        values_list = value_string.split( ", " ) # should be "[32768..65536] 19486","[65536..131072] 1.57837e+06"
        for values_str in values_list:      # value_list should be "[32768..65536] 19486" the first time through
            tmp = values_str.split( ".." )  # should be "[32768", "65536] 19486"
            tmp2 = tmp[1].split( "] " )     # should be "65536","19486"
            stat_list.append( ( str(int(tmp2[0])-1), float(tmp2[1]) ) )
            gsum += float( tmp2[1] )

        return stat_list, gsum

    # calls the weka api to populate a stat
    # This is in a Thread - always, except above
    def _call_weka_api( self, stat, command, category ):     # would schedule() be a better name?
        #logginglevel = log.getEffectiveLevel()
        #log.setLevel(DEBUG)  # increase level to debug 
        #log.debug( f"logging level was {logginglevel}, setting to {DEBUG}")
        log.debug( "stat='{}', command='{}', category='{}'".format( stat, command, category ) )

        # need a lock at this level... how to do it vince
        host = self.hosts.reserve()

        retries = 0
        # no hosts?   Since we can remove hosts that don't respond, we can run out of them
        # this is essentially a terminal error for the program.  They didn't give us any valid hosts,
        # or we can't talk to them (network issues and such)
        while host != None:
            log.debug( "calling Weka API on {}".format(host) )
            try:
                api_return = host.api_obj.weka_api_command( command["method"], **command["parms"] )
            except Exception as exc:
                # something went wrong...  stop talking to this host from here on.
                #log.critical( f"_call_weka_api(): {exc} occurred on host {host.name}" )
                last_hostname = host.name
                self.hosts.remove(host) # remove it from the hostlist iterable

                host = self.hosts.reserve() # try another host; will return None if none left or failure
                #log.error( f"{exc}" )  # test
                log.error( "cluster={}, error {} spawning command {} on host {}. Retrying on {}.".format(
                        self.clustername, exc, str(command), last_hostname, host.name ) )
                continue

            if category == None: 
                self.wekadata[stat] = api_return
            else:
                # check if the category has been initialized
                if category not in self.wekadata:
                    self.wekadata[category] = {}
                self.wekadata[category][stat] = api_return

            self.threaderror = False    # if we had an error before, we succeeded this time!
            self.hosts.release(host)    # release it so it can be used for more queries
            return  # ok, so it worked, we're done.  Success!

        if hostname == None:
            #track = traceback.format_exc()
            #print(track)
            log.error( "no hosts to run on.  cluster:{}, {}".format(self.clustername, command) )
            self.threaderror = True
            return

        # we really should never reach this beause the loop above should have returned or terminated due to hostname == None
        log.critical( "cluster={}: Should never get here".format(self.clustername) )


        # ------------------ end of _call_weka_api() ---------------------

    # start here
    #
    # gather() should run once a minute so it gets fresh stats from the cluster as they update
    #       populates all datastructures with fresh data
    #
    # gather() is PER CLUSTER
    #
    @gather_gauge.time()
    def gather( self ):
        log.info( "gather(): gathering weka data from cluster {}".format(self.clustername) )

        # re-initialize wekadata so changes in the cluster don't leave behind strange things (hosts/nodes that no longer exist, etc)
        self.wekadata = {}

        # to do on-demand gathers instead of every minute;
        #   only gather if we haven't gathered in this minute (since 0 secs has passed)

        thread_runner = simul_threads( len(self.hosts) )    # 1 per host, please

        # did we have errors on the last gather()?  Try re-opening connections to the hosts specified on commandline
        # we delete them from self.hosts, so if the list is shorter, we lost some
        if len(self.hosts) < len(self.orig_hostlist):
            for hostname in self.orig_hostlist:
                if not hostname in self.hosts:
                    try:
                        hostobj = WekaHost(hostname, self.authfile)
                    except:
                        pass
                    else:
                        self.hosts.add(hostobj)
    
        # get info from weka cluster
        for info, command in self.WEKAINFO.items():
            try:
                thread_runner.new( self._call_weka_api, (info, command, None ) )     # always start with the original list of servers from command line
            except:
                log.error( "gather(): error scheduling wekainfo threads for cluster {}".format(self.clustername) )
                return      # bail out if we can't talk to the cluster with this first command

        thread_runner.run()     # kick off threads; wait for them to complete

        if self.threaderror:
            log.critical( "api unable to contact cluster" )
            return

        # reset threading to load balance, if we want to
        if self.loadbalance:
            serverlist = []
            try:
                for host in self.wekadata["hostList"]:
                    # don't try to use inactive or otherwise offline hosts
                    if host["mode"] == "backend" and host["state"] == "ACTIVE" and host["status"] == "UP":
                        # add any we didn't already have
                        hostname = host["hostname"]
                        if not hostname in self.hosts:
                            try:
                                hostobj = WekaHost(hostname, self.authfile)
                            except:
                                pass
                            else:
                                self.hosts.add(hostobj)

            except Exception as exc:
                #track = traceback.format_exc()
                #print(track)
                log.error(f"gather(): {exc} No data retrieved from cluster {self.clustername} - is the cluster down?")
                return      # bail out if we can't talk to the cluster with this first command

        thread_runner = simul_threads(len(self.hosts))   # up the server count - so 1 thread per server in the cluster
        log.info(f"Using {len(self.hosts)} hosts")

        # build maps - need this for decoding data, not collecting it.
        #    do in a try/except block because it can fail if the cluster changes while we're collecting data

        # clear old maps, if any - if nodes come/go this can get funky with old data, so re-create it every time
        self.weka_maps = { "node-host": {}, "node-role": {}, "host-role": {} }       # initial state of maps

        # populate maps
        try:
            for node in self.wekadata["nodeList"]:
                self.weka_maps["node-host"][node["node_id"]] = node["hostname"]
                self.weka_maps["node-role"][node["node_id"]] = node["roles"]    # note - this is a list
            for host in self.wekadata["hostList"]:
                if host["mode"] == "backend":
                    self.weka_maps["host-role"][host["hostname"]] = "server"
                else:
                    self.weka_maps["host-role"][host["hostname"]] = "client"
        except Exception as exc:
            #print(f"EXCEPTION {exc}")
            #track = traceback.format_exc()
            #print(track)
            log.error( "gather(): error building maps. Aborting data gather from cluster {}".format(self.clustername) )
            return


        # schedule a bunch of data gather queries
        for category, stat_dict in self.get_commandlist().items():
            for stat, command in stat_dict.items():
                try:
                    thread_runner.new( self._call_weka_api, (stat, command, category) ) 
                except:
                    log.error( "gather(): error scheduling thread wekastat for cluster {}".format(self.clustername) )

        thread_runner.run()     # schedule the rest of the threads, wait for them

        # if the cluster changed during a gather, this may puke, so just go to the next sample.
        #   One or two missing samples won't hurt

        #  Start filling in the data
        try:
            # determine Cloud Status 
            if self.wekadata["clusterinfo"]["cloud"]["healthy"]: cloudStatus="Healthy"       # must be enabled to be healthy 
            elif self.wekadata["clusterinfo"]["cloud"]["enabled"]:
                cloudStatus="Unhealthy"     # enabled, but unhealthy
            else:
                cloudStatus="Disabled"      # disabled, healthy is meaningless
        except:
            #track = traceback.format_exc()
            #print(track)
            log.error( "gather(): error processing cloud status for cluster {}".format(self.clustername) )

        #
        # start putting the data into the prometheus_client gauges and such
        #
        log.info( "gather(): populating datastructures for cluster {}".format(self.clustername) )

        # set the weka_info Gauge
        try:
            # Weka status indicator
            if (self.wekadata["clusterinfo"]["buckets"]["active"] == self.wekadata["clusterinfo"]["buckets"]["total"] and
                   self.wekadata["clusterinfo"]["drives"]["active"] == self.wekadata["clusterinfo"]["drives"]["total"] and
                   self.wekadata["clusterinfo"]["io_nodes"]["active"] == self.wekadata["clusterinfo"]["io_nodes"]["total"] and
                   self.wekadata["clusterinfo"]["hosts"]["backends"]["active"] == self.wekadata["clusterinfo"]["hosts"]["backends"]["total"]):
               WekaClusterStatus="OK"
            else:
               WekaClusterStatus="WARN"

            # Basic info
            wekacluster = { "cluster": self.clustername, "version": self.wekadata["clusterinfo"]["release"], 
                    "cloud_status": cloudStatus, "license_status":self.wekadata["clusterinfo"]["licensing"]["mode"], 
                    "io_status": self.wekadata["clusterinfo"]["io_status"], "link_layer": self.wekadata["clusterinfo"]["net"]["link_layer"], "status" : WekaClusterStatus }

            metric_objs['wekainfo'].add_metric( labels=wekacluster.keys(), value=wekacluster )

            #log.info( "gather(): cluster name: " + self.wekadata["clusterinfo"]["name"] )
        except:
            #track = traceback.format_exc()
            #print(track)
            log.error( "gather(): error cluster info - aborting populate of cluster {}".format(self.clustername) )
            return


        try:
            # Uptime
            # not sure why, but sometimes this would fail... trim off the microseconds, because we really don't care 
            cluster_time = self._trim_time( self.wekadata["clusterinfo"]["time"]["cluster_time"] )
            start_time = self._trim_time( self.wekadata["clusterinfo"]["io_status_changed_time"] )
            now_obj = datetime.datetime.strptime( cluster_time, "%Y-%m-%dT%H:%M:%S" )
            dt_obj = datetime.datetime.strptime( start_time, "%Y-%m-%dT%H:%M:%S" )
            uptime = now_obj - dt_obj
            metric_objs["wekauptime"].add_metric([self.clustername], uptime.total_seconds())
        except:
            #track = traceback.format_exc()
            #print(track)
            log.error( "gather(): error calculating runtime for cluster {}".format(self.clustername) )


        try:
            # performance overview summary
            # I suppose we could change the gauge names to match the keys, ie: "num_ops" so we could do this in a loop
            #       e: weka_overview_activity_num_ops instead of weka_overview_activity_ops
            for name, parms in self.CLUSTERSTATS.items():
                metric_objs["cluster_stat_"+name].add_metric([self.clustername], self.wekadata["clusterinfo"]["activity"][parms[2]] )

        except:
            #track = traceback.format_exc()
            #print(track)
            log.error( "gather(): error processing performance overview for cluster {}".format(self.clustername) )

        try:
            metric_objs['weka_host_spares'].add_metric([self.clustername], self.wekadata["clusterinfo"]["hot_spare"] )
            metric_objs['weka_host_spares_bytes'].add_metric([self.clustername], self.wekadata["clusterinfo"]["capacity"]["hot_spare_bytes"] )
            metric_objs['weka_drive_storage_total_bytes'].add_metric([self.clustername], self.wekadata["clusterinfo"]["capacity"]["total_bytes"] )
            metric_objs['weka_drive_storage_unprovisioned_bytes'].add_metric([self.clustername], self.wekadata["clusterinfo"]["capacity"]["unprovisioned_bytes"])
            metric_objs['weka_num_servers_active'].add_metric([self.clustername], self.wekadata["clusterinfo"]["hosts"]["backends"]["active"])
            metric_objs['weka_num_servers_total'].add_metric([self.clustername], self.wekadata["clusterinfo"]["hosts"]["backends"]["total"])
            metric_objs['weka_num_clients_active'].add_metric([self.clustername], self.wekadata["clusterinfo"]["hosts"]["clients"]["active"])
            metric_objs['weka_num_clients_total'].add_metric([self.clustername], self.wekadata["clusterinfo"]["hosts"]["clients"]["total"])
            metric_objs['weka_num_drives_active'].add_metric([self.clustername], self.wekadata["clusterinfo"]["drives"]["active"])
            metric_objs['weka_num_drives_total'].add_metric([self.clustername], self.wekadata["clusterinfo"]["drives"]["total"])
        except:
            #track = traceback.format_exc()
            #print(track)
            log.error( "gather(): error processing server overview for cluster {}".format(self.clustername) )

        try:
            # protection status
            rebuildStatus = self.wekadata["clusterinfo"]["rebuild"]
            protectionStateList = rebuildStatus["protectionState"]
            numStates = len( protectionStateList )  # 3 (0,1,2) for 2 parity), or 5 (0,1,2,3,4 for 4 parity)

            for index in range( numStates ):
                metric_objs['weka_protection'].add_metric([self.clustername, str(protectionStateList[index]["numFailures"])], protectionStateList[index]["percent"])

        except:
            #track = traceback.format_exc()
            #print(track)
            log.error( "gather(): error processing protection status for cluster {}".format(self.clustername) )

        try:
            # Filesystem stats
            for fs in self.wekadata["fs_stat"]:
                metric_objs['weka_fs_utilization_percent'].add_metric([self.clustername, fs["name"]], float( fs["used_total"] ) / float( fs["available_total"] ) * 100)
                metric_objs['weka_fs_size_bytes'].add_metric([self.clustername, fs["name"]], fs["available_total"])
                metric_objs['weka_fs_used_bytes'].add_metric([self.clustername, fs["name"]], fs["used_total"])
        except:
            #track = traceback.format_exc()
            #print(track)
            log.error( "gather(): error processing filesystem stats for cluster {}".format(self.clustername) )

        # get all the IO stats...
        #            ['cluster','host_name','host_role','node_id','node_role','category','stat','unit']
        #
        # yes, I know it's convoluted... it was hard to write, so it *should* be hard to read. ;)
        for category, stat_dict in self.get_weka_stat_list().items():
            for stat, nodelist in self.wekadata[category].items():
                unit = stat_dict[stat]
                for node in nodelist:
                    try:
                        hostname = self.weka_maps["node-host"][node["node"]]    # save this because the syntax is gnarly
                        role_list = self.weka_maps["node-role"][node["node"]]
                    except:
                        #track = traceback.format_exc()
                        #print(track)
                        log.error( "gather(): error in maps for cluster {}".format(self.clustername) )
                        continue            # or return?

                    for role in role_list:

                        labelvalues = [ 
                            self.clustername,
                            hostname,
                            self.weka_maps["host-role"][hostname], 
                            node["node"], 
                            role,
                            category,
                            stat,
                            unit ]

                        if unit != "sizes":
                            try:
                                if category == 'ops_nfs':
                                    log.debug( "{} {}".format(stat, node["stat_value"] ) )
                                metric_objs['weka_stats_gauge'].add_metric(labelvalues, node["stat_value"])
                            except:
                                #track = traceback.format_exc()
                                #print(track)
                                log.error( "gather(): error processing io stats for cluster {}".format(self.clustername) )
                        else:   

                            try:
                                value_dict, gsum = self._parse_sizes_values( node["stat_value"] )  # Turn the stat_value into a dict
                                metric_objs['weka_io_histogram'].add_metric( labels=labelvalues, buckets=value_dict, gsum_value=gsum )
                            except:
                                #track = traceback.format_exc()
                                #print(track)
                                log.error( "gather(): error processing io sizes for cluster {}".format(self.clustername) )


    # ------------- end of gather() -------------

    @staticmethod
    def _trim_time( time_string ):
        tmp = time_string.split( '.' )
        return tmp[0]

class WekaHost(object):
    def __init__( self, hostname, tokenfile=None ):
        self.name = hostname
        self.apitoken = tokenfile
        self.api_obj = None
        # do we need a backpointer to the cluster?

        try:
            self.api_obj = WekaApi(self.name, token_file=self.apitoken, timeout=10)
        except Exception as exc:
            log.error(f"{exc}")

        if self.api_obj == None:
            # can't open API session, fail.
            #log.error("WekaHost: unable to open API session")
            raise Exception("Unable to open API session")

    def __str__(self):
        return self.name

    def __repr__(self):
        return self.name

















