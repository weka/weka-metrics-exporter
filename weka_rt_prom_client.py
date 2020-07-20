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
from prometheus_client import start_http_server, Gauge, Info, Summary, Counter
import time, datetime
import json, yaml
import os, sys, stat
import subprocess
import argparse
import threading
import syslog
import os.path
import signal
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
        #print( "starter(): self.running has " + str( len( self.running ) ) + " items, and self.staged has " + str( len( self.staged ) ) + " items" )
        #print( self.num_simultaneous )
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

class wekaCollector():

    # types of things
    wekaInfo = {
        "backendHostList": "cluster host -b",
        "clientHostList": "cluster host -c",
        "nodeList": "cluster nodes",
        "clusterinfo": "status"
        }

    rtStats = {
        'writes_per_second': ['number write operations', ['cluster','host_name','host_role','node_id','node_role','stat']],
        'reads_per_second': ['number read operations', ['cluster','host_name','host_role','node_id','node_role','stat']],
        'write_bytes_per_second': ['write bytes per second', ['cluster','host_name','host_role','node_id','node_role','stat']],
        'read_bytes_per_second': ['read bytes per second', ['cluster','host_name','host_role','node_id','node_role','stat']],
        'write_latency_usecs': ['write latency usecs', ['cluster','host_name','host_role','node_id','node_role','stat']],
        'read_latency_usecs': ['read latency usecs', ['cluster','host_name','host_role','node_id','node_role','stat']],
        'ops_per_second': ['total ops per sec', ['cluster','host_name','host_role','node_id','node_role','stat']],
        'obs_upload_bytes_per_second': ['object upload bytes per sec', ['cluster','host_name','host_role','node_id','node_role','stat']],
        'obs_download_bytes_per_second': ['object download bytes per sec', ['cluster','host_name','host_role','node_id','node_role','stat']],
        'cpu_utilization_percentage': ['cpu utilization', ['cluster','host_name','host_role','node_id','node_role','stat']],
        'L6TX_bytes_per_second': ['network transmit bytes per sec', ['cluster','host_name','host_role','node_id','node_role','stat']],
        'L6RX_bytes_per_second': ['network receive bytes per sec', ['cluster','host_name','host_role','node_id','node_role','stat']]
        }


    # object instance global data
    wekaIOCommands = {}
    collected_data = {}
    singlethreaded = False  # default
    loadbalance = True      # default
    verbose = False         # default
    servers = None
    host = None
    wekadata = {}
    populate_error = 0
    last_refresh = 1000
    # figure out how to handle nodes that have more than one role...
    # maps are node-to-hostname, node-to-noderole (ie: FRONTEND, BACKEND, DRIVES), and host-to-hostrole (ie: server, client)
    weka_maps = { "node-host": {}, "node-role": {}, "host-role": {} }
    gaugelist = {}

    # instrument thyself
    collectinfo_gauge = Gauge('weka_rt_prometheus_collectinfo_gather_seconds', 'Time spent gathering cluster info')
    populate_stats_gauge = Gauge('weka_rt_prometheus_populate_stats_seconds', 'Time spent populating stats')

    def __init__( self, hostname, autohost, verbose ):
        self.host = cycleIterator( [hostname] )
        self.servers = cycleIterator( [hostname] )
        self.loadbalance = autohost
        self.verbose = verbose

        # one gauge to rule them all... all categories and stats are in the labels
        self.gaugelist["weka_realtime_stats"] = Gauge( 'weka_realtime_stats',
            'WekaFS statistics. For more info refer to: https://docs.weka.io/usage/statistics/list-of-statistics',
            ['cluster','host_name','host_role','node_id','node_role','stat'])

        # gauges to track this program's performance, etc
        self.cmd_exec_gauge = Gauge('weka_rt_prometheus_cmd_execute_seconds', 'Time spent gathering statistics', ["stat"])
        #print( json.dumps(self.weka_stat_list, indent=2, sort_keys=True) )
        # ------------- end of __init__() -------------

        
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
    @collectinfo_gauge.time()
    def collectinfo( self ):
        #syslog.syslog( syslog.LOG_INFO, "collectinfo(): collecting data" )
        # get info from weka cluster
        if ( self.last_refresh >= 60 or          # refresh cluster info every minute
                self.populate_error == 1):       # refresh if we got an error populating - meaning something's been added or removed
            print( "populating cluster info" )
            self.populate_error = 0
            self.last_refresh = 0
            thread_runner = simul_threads( len( self.wekaInfo ) )    # have just enough threads to do this work. ??  Maybe should be 1 or 2?
            for info, command in self.wekaInfo.items():
                try:
                    thread_runner.new( self._spawn, (info, command, self.host.next(), None ) )
                except:
                    syslog.syslog( syslog.LOG_ERR, "collectinfo(): error scheduling thread wekainfo" )
                    print( "Error contacting cluster" )
                    return      # bail out if we can't talk to the cluster with this first command

            thread_runner.run()     # kick off threads; wait for them to complete

            # reset threading to load balance, if we want to
            if self.loadbalance:
                serverlist = []
                try:
                    for host in self.wekadata["backendHostList"]:
                        serverlist.append( host["hostname"] )
                    self.servers.reset( serverlist )
                except KeyError:
                    syslog.syslog( syslog.LOG_ERR, "collectinfo(): No data retrieved from cluster - is the cluster down?" )
                    print( "Error No data retrieved from cluster - is the cluster down?" )
                    return      # bail out if we can't talk to the cluster with this first command


            # build maps - need this for decoding data, not collecting it.
            #    do in a try/except block because it can fail if the cluster changes while we're collecting data
            try:
                for node in self.wekadata["nodeList"]:
                    self.weka_maps["node-host"][node["node_id"]] = node["hostname"]
                    #self.weka_maps["node-role"][node["node_id"]] = node["roles"][0]
                    self.weka_maps["node-role"][node["node_id"]] = node["roles"]    # note - this is a list
                for host in self.wekadata["backendHostList"]:
                    self.weka_maps["host-role"][host["hostname"]] = "server"
                for host in self.wekadata["clientHostList"]:
                    self.weka_maps["host-role"][host["hostname"]] = "client"
            except:
                syslog.syslog( syslog.LOG_ERR, "collectinfo(): error building maps. Aborting data collection." )
                print( "Error building maps!" )
                return

        try:
            self._spawn("weka_realtime_stats", "stats realtime -R", self.servers.next(), None)
        except:
            syslog.syslog( syslog.LOG_ERR, "collectinfo(): error spawning weka stats" )
            print( "Error error spawning weka stats" )

        self.last_refresh += 1  # keep track of how many times we've done this so we can refresh cluster state now and then

        # ------------- end of collectinfo() -------------

    @populate_stats_gauge.time()
    def populate_stats( self ):
        if self.verbose:
            print( "starting populate_stats()" )
        #syslog.syslog( syslog.LOG_INFO, "populate_stats(): populating statistics" )
        # if the cluster changed during a collection, this will puke, so just go to the next sample.
        #   One or two missing samples won't hurt

        try:
            # get all the IO stats...
            #      ['cluster','host_name','host_role','node_id','node_role','stat'])
            #            old ['cluster','host_name','host_role','node_id','node_role','category','stat','unit']
            #
            # yes, I know it's convoluted... it was hard to write, so it *should* be hard to read. ;)

            for node in self.wekadata["weka_realtime_stats"]:
                hostname = self.weka_maps["node-host"][node["node_id"]]    # save this because the syntax is gnarly
                #node_role = self.weka_maps["node-role"][node["node_id"]]
                for stat in node:
                    for role in self.weka_maps["node-role"][node["node_id"]]:  # when role is a list
                        #print( "stat = " + stat )
                        if not stat == "node_id":
                            self.gaugelist["weka_realtime_stats"].labels( 
                                self.wekadata["clusterinfo"]["name"], 
                                hostname,
                                self.weka_maps["host-role"][hostname], 
                                node["node_id"], 
                                role,
                                stat ).set( node[stat] )

        except:
            syslog.syslog( syslog.LOG_ERR, "populate_stats(): error processing io stats" )
            print( "Error processing io stats!" )
            self.populate_error = 1

        if self.verbose:
            print( "ending populate_stats()" )

        # ------------- end of populate_stats() -------------

    def _trim_time( self, time_string ):
        tmp = time_string.split( '.' )
        return tmp[0]

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
#    parser.add_argument("-c", "--configfile", dest='configfile', default="./weka_prom.yml", help="override ./weka_prom.yml as config file")
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
    wekacollect = wekaCollector( args.wekahost, args.autohost, args.verbose )

    # debugging
    #all_objects = muppy.get_objects()

    #
    # Start up the server to expose the metrics.
    #
    start_http_server(int(args.port))
    # Generate some requests.
    while True:
        # collect the data (ie: run weka commands)
        wekacollect.collectinfo()
        
        # populate/update the gauge objects with the data
        wekacollect.populate_stats()

        # weka updates stats at the top of the minute.  Wait until 1 sec past to ensure we have new stats
        now = time.time()
        time_to_sleep = abs(1 - (now - int( now ))) + .01
        #print( now )
        #print( time_to_sleep )

        # muppy memory utilization
        #sum1 = summary.summarize(all_objects)
        #summary.print_(sum1)

        # sleep until next weka stats update, so we don't waste cpu
        #time.sleep(secs_to_next_min +1)
        time.sleep( time_to_sleep )



