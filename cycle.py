
#
# cycle module - implement cycleIterators - a circular list
#

# author: Vince Fleming, vince@weka.io

from threading import Lock

# cycleIterator - iterate over a list, maintaining state between calls
# used to implement --autohost
class cycleIterator():
    def __init__( self, list ):
        self._lock = Lock() # make it re-entrant (thread-safe)
        self.list = list
        self.current = 0

    # return next item in the list
    def next( self ):
        with self._lock:
            if len( self.list ) == 0:
                return None # nothing in the list
            item = self.list[self.current]
            self.current += 1
            if self.current >= len( self.list ):    # cycle back to beginning
                self.current = 0
            return item

    # reset the list to something new
    def reset( self, list ):
        with self._lock:
            self.list = list
            if self.current >= len( self.list ):    # handle case where a node may have left the cluster
                self.current = 0

    def count( self ):
        with self._lock:
            return len( self.list )

    def remove( self, item ):
        with self._lock:
            self.list.remove( item )

    def __str__( self ):
        with self._lock:
            return "list=" + str( self.list ) + ", current=" + str(self.current)


