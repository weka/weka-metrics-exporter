#! /usr/bin/env python3

# Weka Prometheus client
# Vince Fleming
# vince@weka.io
#
# Note - this is an EXAMPLE of how to gather statistics about Weka, and it totally unsupported, dude.
# However, feel free to contact Vince with feedback, questions, and enhancement requests; he may be in the mood to help
#
# This script assumes Python 2.7, but should work with newer versions of Python
#
# Installation instructions:
#   install pip (yum -y install python2-pip)
#
#   install prometheus_client (pip install prometheus_client)
#   install pyyaml (pip install pyyaml) - note aws does not always have a new enough version
#
#   run this script.  You *should* start it via a service/systemd (ie: systemctl)
#       maybe someday we'll include the config files for systemd...
#
#   Add this server:8000 to Prometheus's .yml configuration

import prometheus_client
from prometheus_client import start_http_server, Gauge, Info, Summary, Counter, Histogram, REGISTRY
from prometheus_client.core import GaugeMetricFamily, InfoMetricFamily, GaugeHistogramMetricFamily
import time, datetime
import json, yaml
import os, sys, stat
import subprocess
import argparse
import threading
from threading import Lock
import syslog
import os.path
import signal
import traceback
#from pympler import muppy, summary


# ---------------- start of threader definition ------------
# manage threads - start only num_simultaneous threads at a time
#
# You should add all the threads, then start all via run().
# ***Execution order is random***
class simul_threads():

    def __init__( self, num_simultaneous ):
        self.num_simultaneous = num_simultaneous    # max number of threads to run at any given time
        self.ids = 0                # thread id... a counter that increases over time
        self.staged = {}            # threads that need to be run - dict of {threadid:thread_object}
        self.running = {}           # currently running threads that will need to be reaped - dict (same as staged)
        self.dead = {}

    # create a thread and put it in the list of threads
    def new( self, function, funcargs=None ):
        self.ids += 1
        if funcargs == None:
            self.staged[self.ids] = threading.Thread( target=function )
        else:
            self.staged[self.ids] = threading.Thread( target=function, args=funcargs )

    def status( self ):
        print( "Current status of threads:" )
        for threadid, thread in self.running.items():
            print( "Threadid: " + str( threadid ) + " is " + ("alive" if thread.is_alive() else "dead") )
        for threadid, thread in self.staged.items():
            print( "Threadid: " + str( threadid ) + " is staged" )
        return len( self.staged ) + len( self.running )

    # look for threads that need reaping, start next thread
    def reaper( self ):
        for threadid, thread in self.running.items():
            if not thread.is_alive():
                thread.join()                   # reap it (wait for it)
                self.dead[threadid] = thread    # note that it's dead/done

        # remove them from the running list
        for threadid, thread in self.dead.items():
                self.running.pop( threadid )    # delete it from the running list

        self.dead = {}                          # reset dead list, as we're done with those threads

    # start threads, but only a few at a time
    def starter( self ):
        # only allow num_simultaneous threads to run at one time
        #print "starter(): self.running has " + str( len( self.running ) ) + " items, and self.staged has " + str( len( self.staged ) ) + " items"
        #print self.num_simultaneous
        while len( self.running ) < self.num_simultaneous and len( self.staged ) > 0:
            threadid, thread = self.staged.popitem()    # take one off the staged list
            thread.start()                              # start it
            self.running[threadid] = thread             # put it on the running list

    def num_active( self ):
        return len( self.running )

    def num_staged( self ):
        return len( self.staged )

    # run all threads, wait for all to complete
    def run( self ):
        while len( self.staged ) + len( self.running ) > 0:
            self.reaper()       # reap any dead threads
            self.starter()      # kick off threads
            time.sleep( 0.1 )     # limit CPU use

# ---------------- end of threader definition ------------
# cycleIterator - iterate over a list, maintaining state between calls
# used to implement --autohost
class cycleIterator():
    def __init__( self, list ):
        self.list = list
        self.current = 0

    # return next item in the list
    def next( self ):
        item = self.list[self.current]
        self.current += 1
        if self.current >= len( self.list ):    # cycle back to beginning
            self.current = 0
        return item

    # reset the list to something new
    def reset( self, list ):
        self.list = list
        if self.current >= len( self.list ):    # handle case where a node may have left the cluster
            self.current = 0

    def count( self ):
        return len( self.list )

    def __str__( self ):
        return "list=" + str( self.list ) + ", current=" + str(self.current)

# ---------------- end of cycleIterator definition ------------

class WekaIOHistogram(Histogram):
    def multi_observe( self, iosize, value ):
        """Observe the given amount."""
        self._sum.inc(iosize * value)
        for i, bound in enumerate(self._upper_bounds):
            if float(iosize) <= bound:
                self._buckets[i].inc(value)
                break

# ---------------- end of WekaIOHistogram definition ------------

class wekaCollector():

    # types of things
    wekaInfo = {
        "backendHostList": "cluster host -b",
        "clientHostList": "cluster host -c",
        "clusterinfo": "status",
        "nodeList": "cluster nodes",
        "fs_stat": "fs"
        }

    # this comes from a yaml file
    weka_stat_list = {}
        # category: {{ stat:unit}, {stat:unit}}

    clusterStats = {
        'weka_overview_activity_ops': ['Weka IO Summary number of operations', ['cluster'], 'num_ops'],
        'weka_overview_activity_read_iops': ['Weka IO Summary number of read operations', ['cluster'], 'num_reads'],
        'weka_overview_activity_read_bytespersec': ['Weka IO Summary read rate', ['cluster'], 'sum_bytes_read'],
        'weka_overview_activity_write_iops': ['Weka IO Summary number of write operations', ['cluster'], 'num_writes'],
        'weka_overview_activity_write_bytespersec': ['Weka IO Summary write rate', ['cluster'], 'sum_bytes_written'],
        'weka_overview_activity_object_download_bytespersec': ['Weka IO Summary Object Download BPS', ['cluster'], 'obs_download_bytes_per_second'],
        'weka_overview_activity_object_upload_bytespersec': ['Weka IO Summary Object Upload BPS', ['cluster'], 'obs_upload_bytes_per_second']
        }


    # object instance global data
    wekaIOCommands = {}
    collected_data = {}
    histograms = {}
    singlethreaded = False  # default
    loadbalance = True      # default
    verbose = False         # default
    servers = None
    host = None
    wekadata = {}
    buckets = []
    # figure out how to handle nodes that have more than one role...
    # maps are node-to-hostname, node-to-noderole (ie: FRONTEND, BACKEND, DRIVES), and host-to-hostrole (ie: server, client)
    weka_maps = { "node-host": {}, "node-role": {}, "host-role": {} }
    _access_lock = Lock()

    # instrument thyself
    weka_metrics_gather_gauge = Gauge('weka_metrics_exporter_weka_metrics_gather_seconds', 'Time spent gathering cluster info')
    prom_collect_gauge = Gauge('weka_metrics_exporter_prom_collect_seconds', 'Time spent in Prometheus collect')

    def __init__( self, hostname, autohost, verbose, configfile ):
        self.host = cycleIterator( [hostname] )
        self.servers = cycleIterator( [hostname] )
        self.loadbalance = autohost
        self.verbose = verbose

        # load the config file
        self.weka_stat_list = self._load_config( configfile )

        # set up commands to get stats defined in config file
        # category: {{ stat:unit}, {stat:unit}}
        for category, stat_dict in self.weka_stat_list.items():
            for stat, unit in stat_dict.items():
                # have to create the category keys, so do it with a try: block
                try:
                    self.wekaIOCommands[category][stat] = "stats --start-time -1m --stat "+stat+" --category "+category+" -Z -R --per-node"
                except KeyError:
                    self.wekaIOCommands[category] = {}
                    self.wekaIOCommands[category][stat] = "stats --start-time -1m --stat "+stat+" --category "+category+" -Z -R --per-node"


        # set up buckets, [4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576, 2097152, 4194304, 8388608, 16777216, 33554432, 67108864, 134217728, inf]
        for i in range(12,12+16):
            self.buckets.append( 2 ** i )

        self.buckets.append( float("inf") )
        #print( self.buckets )

        # gauges to track this program's performance, etc
        self.cmd_exec_gauge = Gauge('weka_metrics_exporter_cmd_execute_seconds', 'Time spent gathering statistics', ["stat"])
        #print json.dumps(self.weka_stat_list, indent=2, sort_keys=True)
        # ------------- end of __init__() -------------

    def _load_config( self, inputfile ):
        with open( inputfile ) as f:
            try:
                return yaml.load(f, Loader=yaml.FullLoader)
            except AttributeError:
                return yaml.load(f)
        
    def _parse_sizes_values( self, value_string ):  # returns list of tuples of [(iosize,value),(iosize,value),...], and their sum
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

    def _spawn( self, stat, command, host, category ):     # would schedule() be a better name?
        # spawn a command
        full_command = "weka " + command + " -J -H " + host
        if category == None:
            gaugekey = stat
        else:
            gaugekey = category+":"+stat

        if self.verbose:
            print( "executing: " + full_command )
        with self.cmd_exec_gauge.labels(gaugekey).time() as timer:
            try:
                if category == None: ### think on this
                    self.wekadata[stat] = json.loads( subprocess.check_output( full_command, shell=True ) )
                else:
                    try:
                        self.wekadata[category][stat] = json.loads( subprocess.check_output( full_command, shell=True ) )
                    except KeyError:
                        self.wekadata[category] = {}
                        self.wekadata[category][stat] = json.loads( subprocess.check_output( full_command, shell=True ) )
            except:
                syslog.syslog( syslog.LOG_ERR, "_spawn(): error spawning command " + full_command )
                print( "Error spawning command " + full_command )
                #self.wekadata[stat] = []    # hmm... not sure we want to do this


    # start here
    #
    # weka_metrics_gather() should run once a minute so it gets fresh stats from the cluster as they update
    #       populates all datastructures with fresh data
    #
    @weka_metrics_gather_gauge.time()
    def weka_metrics_gather( self ):
        with self._access_lock:
            syslog.syslog( syslog.LOG_INFO, "weka_metrics_gather(): collecting weka data" )
            thread_runner = simul_threads( len( self.wekaInfo ) )    # have just enough threads to do this work. ??  Maybe should be 1 or 2?

            # re-initialize wekadata so changes in the cluster don't leave behind strange things (hosts/nodes that no longer exist, etc)
            self.wekadata = {}

            # get info from weka cluster
            for info, command in self.wekaInfo.items():
                try:
                    thread_runner.new( self._spawn, (info, command, self.host.next(), None ) )
                except:
                    syslog.syslog( syslog.LOG_ERR, "weka_metrics_gather(): error scheduling thread wekainfo" )
                    print( "Error contacting cluster" )
                    return      # bail out if we can't talk to the cluster with this first command

            thread_runner.run()     # kick off threads; wait for them to complete

            # reset threading to load balance, if we want to
            if self.loadbalance:
                serverlist = []
                try:
                    for host in self.wekadata["backendHostList"]:
                        # don't try to collect from inactive or otherwise offline hosts
                        if host["state"] == "ACTIVE":
                            serverlist.append( host["hostname"] )
                    self.servers.reset( serverlist )
                except KeyError:
                    syslog.syslog( syslog.LOG_ERR, "weka_metrics_gather(): No data retrieved from cluster - is the cluster down?" )
                    print( "Error No data retrieved from cluster - is the cluster down?" )
                    return      # bail out if we can't talk to the cluster with this first command

            thread_runner = simul_threads( self.servers.count() )   # up the server count

            # build maps - need this for decoding data, not collecting it.
            #    do in a try/except block because it can fail if the cluster changes while we're collecting data

            # clear old maps, if any - if nodes come/go this can get funky with old data, so re-create it every time
            self.weka_maps = { "node-host": {}, "node-role": {}, "host-role": {} }       # initial state of maps

            # populate maps
            try:
                for node in self.wekadata["nodeList"]:
                    self.weka_maps["node-host"][node["node_id"]] = node["hostname"]
                    self.weka_maps["node-role"][node["node_id"]] = node["roles"]    # note - this is a list
                for host in self.wekadata["backendHostList"]:
                    self.weka_maps["host-role"][host["hostname"]] = "server"
                for host in self.wekadata["clientHostList"]:
                    self.weka_maps["host-role"][host["hostname"]] = "client"
            except:
                syslog.syslog( syslog.LOG_ERR, "weka_metrics_gather(): error building maps. Aborting data collection." )
                print( "Error building maps!" )
                return


            # schedule a bunch of data collection queries
            for category, stat_dict in self.wekaIOCommands.items():
                for stat, command in stat_dict.items():
                    try:
                        thread_runner.new( self._spawn, (stat, command, self.servers.next(), category) ) 
                    except:
                        syslog.syslog( syslog.LOG_ERR, "weka_metrics_gather(): error scheduling thread wekastat" )
                        print( "Error spawning thread" )

            thread_runner.run()     # schedule the rest of the threads, wait for them

        # ------------- end of weka_metrics_gather() -------------

    @prom_collect_gauge.time()
    def prom_collect( self ):
        with self._access_lock:
            syslog.syslog( syslog.LOG_INFO, "prom_collect(): collecting statistics" )

            # if the cluster changed during a collection, this may puke, so just go to the next sample.
            #   One or two missing samples won't hurt

            # have we had our first collection yet?
            if self.wekadata == {}:
                return

            try:
                # determine Cloud Status 
                if self.wekadata["clusterinfo"]["cloud"]["healthy"]: cloudStatus="Healthy"       # must be enabled to be healthy 
                elif self.wekadata["clusterinfo"]["cloud"]["enabled"]:
                    cloudStatus="Unhealthy"     # enabled, but unhealthy
                else:
                    cloudStatus="Disabled"      # disabled, healthy is meaningless
            except:
                track = traceback.format_exc()
                print(track)
                syslog.syslog( syslog.LOG_ERR, "prom_collect(): error processing cloud status" )
                print( "Error processing Cloud Status!" )

            # set the weka_info Gauge
            try:
                # Basic info
                wekacluster = { "cluster": self.wekadata["clusterinfo"]["name"], "version": self.wekadata["clusterinfo"]["release"], 
                        "cloud_status": cloudStatus, "license_status":self.wekadata["clusterinfo"]["licensing"]["mode"], 
                        "io_status": self.wekadata["clusterinfo"]["io_status"], "link_layer": self.wekadata["clusterinfo"]["net"]["link_layer"] }

                wekainfo = InfoMetricFamily( 'weka', "Information about the Weka cluster", value=wekacluster )

                syslog.syslog( syslog.LOG_INFO, "weka_metrics_gather(): cluster name: " + self.wekadata["clusterinfo"]["name"] )
            except:
                track = traceback.format_exc()
                print(track)
                syslog.syslog( syslog.LOG_ERR, "prom_collect(): error cluster info - aborting populate" )
                print( "Error processing Basic info!" )
                return

            yield wekainfo

            try:
                # Weka status indicator
                if (self.wekadata["clusterinfo"]["buckets"]["active"] == self.wekadata["clusterinfo"]["buckets"]["total"] and
                       self.wekadata["clusterinfo"]["drives"]["active"] == self.wekadata["clusterinfo"]["drives"]["total"] and
                       self.wekadata["clusterinfo"]["io_nodes"]["active"] == self.wekadata["clusterinfo"]["io_nodes"]["total"] and
                       self.wekadata["clusterinfo"]["hosts"]["backends"]["active"] == self.wekadata["clusterinfo"]["hosts"]["backends"]["total"]):
                   WekaClusterStatus="OK"
                else:
                   WekaClusterStatus="WARN"
                        
                wekstatus = GaugeMetricFamily( 'weka_status', "Weka cluster status (OK, etc)", labels=["cluster","status"] )
                wekstatus.add_metric([self.wekadata["clusterinfo"]["name"], WekaClusterStatus], 0)
            except:
                track = traceback.format_exc()
                print(track)
                syslog.syslog( syslog.LOG_ERR, "prom_collect(): error processing weka status" )
                print( "Error processing weka status indicator!" )

            yield wekstatus

            try:
                # Uptime
                # not sure why, but sometimes this would fail... trim off the microseconds, because we really don't care 
                cluster_time = self._trim_time( self.wekadata["clusterinfo"]["time"]["cluster_time"] )
                start_time = self._trim_time( self.wekadata["clusterinfo"]["io_status_changed_time"] )
                now_obj = datetime.datetime.strptime( cluster_time, "%Y-%m-%dT%H:%M:%S" )
                dt_obj = datetime.datetime.strptime( start_time, "%Y-%m-%dT%H:%M:%S" )
                uptime = now_obj - dt_obj
                wekauptime = GaugeMetricFamily( 'weka_uptime', "Weka cluster uptime", labels=["cluster"] )
                wekauptime.add_metric([self.wekadata["clusterinfo"]["name"]], uptime.total_seconds())
            except:
                track = traceback.format_exc()
                print(track)
                syslog.syslog( syslog.LOG_ERR, "prom_collect(): error calculating runtime" )
                print( "Error processing uptime!" )

            yield wekauptime

            try:
                # performance overview summary
                # I suppose we could change the gauge names to match the keys, ie: "num_ops" so we could do this in a loop
                #       e: weka_overview_activity_num_ops instead of weka_overview_activity_ops
                for name, parms in self.clusterStats.items():
                    cluster_stat = GaugeMetricFamily( name, parms[0], labels=parms[1] )
                    cluster_stat.add_metric([self.wekadata["clusterinfo"]["name"]], self.wekadata["clusterinfo"]["activity"][parms[2]] )
                    yield cluster_stat

            except:
                track = traceback.format_exc()
                print(track)
                syslog.syslog( syslog.LOG_ERR, "prom_collect(): error processing performance overview" )
                print( "Error processing performance overview!" )

            try:

                    cluster_info = GaugeMetricFamily( 'weka_host_spares', 'Weka cluster # of hot spares', labels=["cluster"] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"]], self.wekadata["clusterinfo"]["hot_spare"] )
                    yield cluster_info

                    cluster_info = GaugeMetricFamily( 'weka_host_spares_bytes', 'Weka capacity of hot spares', labels=["cluster"] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"]], self.wekadata["clusterinfo"]["capacity"]["hot_spare_bytes"] )
                    yield cluster_info

                    cluster_info = GaugeMetricFamily( 'weka_drive_storage_total_bytes', 'Weka total drive capacity', labels=["cluster"] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"]], self.wekadata["clusterinfo"]["capacity"]["total_bytes"] )
                    yield cluster_info

                    cluster_info = GaugeMetricFamily( 'weka_drive_storage_unprovisioned_bytes', 'Weka unprovisioned drive capacity', labels=["cluster"] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"]], self.wekadata["clusterinfo"]["capacity"]["unprovisioned_bytes"])
                    yield cluster_info

                    cluster_info = GaugeMetricFamily( 'weka_num_servers_active', 'Number of active weka servers', labels=["cluster"] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"]], self.wekadata["clusterinfo"]["hosts"]["backends"]["active"])
                    yield cluster_info

                    cluster_info = GaugeMetricFamily( 'weka_num_servers_total', 'Total number of weka servers', labels=["cluster"] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"]], self.wekadata["clusterinfo"]["hosts"]["backends"]["total"])
                    yield cluster_info

                    cluster_info = GaugeMetricFamily( 'weka_num_clients_active', 'Number of active weka clients', labels=["cluster"] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"]], self.wekadata["clusterinfo"]["hosts"]["clients"]["active"])
                    yield cluster_info

                    cluster_info = GaugeMetricFamily( 'weka_num_clients_total', 'Total number of weka clients', labels=["cluster"] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"]], self.wekadata["clusterinfo"]["hosts"]["clients"]["total"])
                    yield cluster_info

                    cluster_info = GaugeMetricFamily( 'weka_num_drives_active', 'Number of active weka drives', labels=["cluster"] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"]], self.wekadata["clusterinfo"]["drives"]["active"])
                    yield cluster_info

                    cluster_info = GaugeMetricFamily( 'weka_num_drives_total', 'Total number of weka drives', labels=["cluster"] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"]], self.wekadata["clusterinfo"]["drives"]["total"])
                    yield cluster_info

            except:
                track = traceback.format_exc()
                print(track)
                syslog.syslog( syslog.LOG_ERR, "prom_collect(): error processing server overview" )
                print( "Error processing server overview!" )

            try:
                # protection status
                rebuildStatus = self.wekadata["clusterinfo"]["rebuild"]
                protectionStateList = rebuildStatus["protectionState"]
                numStates = len( protectionStateList )  # 3 (0,1,2) for 2 parity), or 5 (0,1,2,3,4 for 4 parity)

                cluster_info = GaugeMetricFamily( 'weka_protection', 'Weka Data Protection Status', labels=["cluster",'numFailures'] )
                for index in range( numStates ):
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"], str(protectionStateList[index]["numFailures"])], protectionStateList[index]["percent"])
                yield cluster_info

            except:
                track = traceback.format_exc()
                print(track)
                syslog.syslog( syslog.LOG_ERR, "prom_collect(): error processing protection status" )
                print( "Error processing protection status!" )

            try:
                # Filesystem stats
                for fs in self.wekadata["fs_stat"]:
                    cluster_info = GaugeMetricFamily( 'weka_fs_utilization_percent', 'Filesystem % used', labels=["cluster",'fsname'] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"], fs["name"]], float( fs["used_total"] ) / float( fs["available_total"] ) * 100)
                    yield cluster_info

                    cluster_info = GaugeMetricFamily( 'weka_fs_size_bytes', 'Filesystem size', labels=["cluster",'name'] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"], fs["name"]], fs["available_total"])
                    yield cluster_info

                    cluster_info = GaugeMetricFamily( 'weka_fs_used_bytes', 'Filesystem used capacity',
                            labels=["cluster",'name'] )
                    cluster_info.add_metric([self.wekadata["clusterinfo"]["name"], fs["name"]], fs["used_total"])
                    yield cluster_info

            except:
                track = traceback.format_exc()
                print(track)
                syslog.syslog( syslog.LOG_ERR, "prom_collect(): error processing filesystem stats" )
                print( "Error processing filesystem stats!" )


            # general metrics Gauge
            weka_stats_gauge = GaugeMetricFamily('weka_stats', 'WekaFS statistics. For more info refer to: https://docs.weka.io/usage/statistics/list-of-statistics',
                    labels=['cluster','host_name','host_role','node_id','node_role','category','stat','unit'])

            # histogram of io blocksizes - for all histogram metrics
            weka_io_histogram = GaugeHistogramMetricFamily( "weka_blocksize", "weka blocksize distribution histogram", 
                    labels=['cluster','host_name','host_role','node_id','node_role','category','stat','unit'] )

            # get all the IO stats...
            #            ['cluster','host_name','host_role','node_id','node_role','category','stat','unit']
            #
            # yes, I know it's convoluted... it was hard to write, so it *should* be hard to read. ;)
            for category, stat_dict in self.weka_stat_list.items():
                for stat, nodelist in self.wekadata[category].items():
                    unit = stat_dict[stat]
                    for node in nodelist:
                        try:
                            hostname = self.weka_maps["node-host"][node["node"]]    # save this because the syntax is gnarly
                            role_list = self.weka_maps["node-role"][node["node"]]
                        except:
                            track = traceback.format_exc()
                            print(track)
                            syslog.syslog( syslog.LOG_ERR, "prom_collect(): error in maps" )
                            print( "Error in maps" )
                            continue            # or return?

                        for role in role_list:

                            labelvalues = [ 
                                self.wekadata["clusterinfo"]["name"], 
                                hostname,
                                self.weka_maps["host-role"][hostname], 
                                node["node"], 
                                role,
                                category,
                                stat,
                                unit ]

                            if unit != "sizes":
                                try:
                                    weka_stats_gauge.add_metric(labelvalues, node["stat_value"])
                                except:
                                    track = traceback.format_exc()
                                    print(track)
                                    syslog.syslog( syslog.LOG_ERR, "prom_collect(): error processing io stats" )
                                    print( "Error processing io stats!" )
                            else:   
                                #weka_io_histogram = GaugeHistogramMetricFamily( "weka_blocksize_"+category+"_"+stat, "weka "+category+" "+stat+" blocksizes", # should this be above?
                                #    labels=['cluster','host_name','host_role','node_id','node_role','category','stat','unit'] )

                                try:
                                    value_dict, gsum = self._parse_sizes_values( node["stat_value"] )  # Turn the stat_value into a dict
                                    #print( value_dict )
                                    #print( gsum )
                                    weka_io_histogram.add_metric( labels=labelvalues, buckets=value_dict, gsum_value=gsum )
                                except:
                                    track = traceback.format_exc()
                                    print(track)
                                    syslog.syslog( syslog.LOG_ERR, "prom_collect(): error processing io sizes" )
                                    print( "Error processing io sizes!" )

            yield weka_stats_gauge
            yield weka_io_histogram

        # ------------- end of prom_collect() -------------

    def _trim_time( self, time_string ):
        tmp = time_string.split( '.' )
        return tmp[0]

# our prometheus collector
class CustomCollector(object):
    weka = None
    def __init__(self, weka):
        self.weka = weka
        pass

    def collect(self):
        if self.weka.verbose:
            print( "prom collecting" )
        return self.weka.prom_collect()

# classless functions
def sigterm_handler(signal, frame):
    # save the state here or do whatever you want
    syslog.syslog( syslog.LOG_INFO, "SIGTERM received, exiting" )
    print('SIGTERM received, exiting')
    sys.exit(0)

def sigint_handler(signal, frame):
    # save the state here or do whatever you want
    syslog.syslog( syslog.LOG_INFO, "SIGINT received, exiting" )
    print('SIGINT received, exiting')
    sys.exit(0)

def sighup_handler(signal, frame):
    # save the state here or do whatever you want
    syslog.syslog( syslog.LOG_INFO, "SIGHUP received, exiting" )
    print('SIGHUP received, exiting')
    sys.exit(0)

#
# Main
#

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Prometheus Client for Weka clusters")
    parser.add_argument("-c", "--configfile", dest='configfile', default="./weka-metrics-exporter.yml", help="override ./weka-metrics-exporter.yml as config file")
    parser.add_argument("-p", "--port", dest='port', default="8001", help="TCP port number to listen on")
    parser.add_argument("-H", "--HOST", dest='wekahost', default="localhost", help="Specify the Weka host (hostname/ip) to collect stats from")
    parser.add_argument("-a", "--autohost", dest='autohost', default=False, action="store_true", help="Automatically load balance queries over backend hosts" )
    parser.add_argument("-v", "--verbose", dest='verbose', default=False, action="store_true", help="Enable verbose output" )
    args = parser.parse_args()

    # start the syslogger
    syslog.openlog( os.path.basename( sys.argv[0] ), logoption=syslog.LOG_PID )
    syslog.syslog( syslog.LOG_INFO, "starting" )

    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGINT, sigint_handler)
    signal.signal(signal.SIGHUP, sighup_handler)

    # create the wekaCollector object
    wekacollect = wekaCollector( args.wekahost, args.autohost, args.verbose, args.configfile )

    # debugging
    #all_objects = muppy.get_objects()

    # sleep to the top of the minute to ensure our first data collection is valid
    now = time.time()
    secs_to_next_min = 60 - (now % 60)
    print( "sleeping for " + str(secs_to_next_min +1) + "secs before first data collection" )
    time.sleep(secs_to_next_min +1)

    # perform first data collection before we open web page
    wekacollect.weka_metrics_gather()

    #
    # Start up the server to expose the metrics.
    #
    start_http_server(int(args.port))

    REGISTRY.register( CustomCollector(wekacollect) )
    # Generate some requests.
    while True:

        # weka updates stats at the top of the minute.  Wait until 1 sec past to ensure we have new stats
        now = time.time()
        secs_to_next_min = 60 - (now % 60)

        # muppy memory utilization
        #sum1 = summary.summarize(all_objects)
        #summary.print_(sum1)

        # sleep until next weka stats update, so we don't waste cpu
        if args.verbose:
            print( "sleeping for " + str(secs_to_next_min +1) + "secs" )
        time.sleep(secs_to_next_min +1)

        # populate/update the gauge objects with the data
        if args.verbose:
            print( "collecting weka info" )
        wekacollect.weka_metrics_gather()


