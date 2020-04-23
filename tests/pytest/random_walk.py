#!/usr/bin/python3
###################################################################
#           Copyright (c) 2016 by TAOS Technologies, Inc.
#                     All rights reserved.
#
#  This file is proprietary and confidential to TAOS Technologies.
#  No part of this file may be reproduced, stored, transmitted,
#  disclosed or used in any form or by any means other than as
#  expressly provided by the written permission from Jianhui Tao
#
###################################################################

# -*- coding: utf-8 -*-
import sys
import getopt

import threading
import random
import logging

from util.log import *
from util.dnodes import *
from util.cases import *
from util.sql import *

import taos

# Constants
LOGGING_LEVEL = logging.DEBUG

def runThread(workerThread):
    logger.info("Running Thread: {}".format(workerThread.tid))
    workerThread.run()

class WorkerThread:
    def __init__(self, pool, tid): # note: main thread context!
        self.curStep = -1 
        self.pool = pool
        self.tid = tid
        # self.threadIdent = threading.get_ident()
        self.thread = threading.Thread(target=runThread, args=(self,))
        self.stepGate = threading.Condition()

    def start(self):
        self.thread.start()  # AFTER the thread is recorded

    def run(self):
        # initialization after thread starts, in the thread context
        self.isSleeping = False

        while self.curStep < self.pool.maxSteps:
            # stepNo = self.pool.waitForStep() # Step to run
            self.crossStepGate()  # self.curStep will get incremented
            self.doWork()

    def verifyThreadSelf(self): # ensure we are called by this own thread
        if ( threading.get_ident() != self.thread.ident ): 
            raise RuntimeError("Unexpectly called from other threads")

    def verifyThreadMain(self): # ensure we are called by the main thread
        if ( threading.get_ident() != threading.main_thread().ident ): 
            raise RuntimeError("Unexpectly called from other threads")

    def verifyThreadAlive(self):
        if ( not self.thread.is_alive() ):
            raise RuntimeError("Unexpected dead thread")

    def verifyIsSleeping(self, isSleeping):
        if ( isSleeping != self.isSleeping ):
            raise RuntimeError("Unexpected thread sleep status")

    def crossStepGate(self):
        self.verifyThreadAlive()
        self.verifyThreadSelf() # only allowed by ourselves
        self.verifyIsSleeping(False) # has to be awake

        logger.debug("Worker thread {} going to sleep".format(self.tid))
        self.isSleeping = True # TODO: maybe too early?
        self.pool.reportThreadWaiting() # TODO: this triggers the main thread, TOO early
        
        # Actually going to sleep
        self.stepGate.acquire() # acquire lock immediately     
        self.stepGate.wait() # release and then acquire
        self.stepGate.release() # release
        
        logger.debug("Worker thread {} woke up".format(self.tid))
        # Someone will wake us up here
        self.curStep += 1  # off to a new step...

    def tapStepGate(self): # give it a tap, release the thread waiting there
        self.verifyThreadAlive()
        self.verifyThreadMain() # only allowed for main thread
        self.verifyIsSleeping(True) # has to be sleeping

        logger.debug("Tapping worker thread {}".format(self.tid))
        self.stepGate.acquire()
        # logger.debug("Tapping worker thread {}, lock acquired".format(self.tid))
        self.stepGate.notify() # wake up!
        # logger.debug("Tapping worker thread {}, notified!".format(self.tid))
        self.isSleeping = False # No race condition for sure
        self.stepGate.release() # this finishes before .wait() can return
        # logger.debug("Tapping worker thread {}, lock released".format(self.tid))
        time.sleep(0) # let the released thread run a bit, IMPORTANT, do it after release

    def doWork(self):
        logger.info("  Step {}, thread {}: ".format(self.curStep, self.tid))
        self.pool.dispatcher.doWork()
        

# We define a class to run a number of threads in locking steps.
class SteppingThreadPool:
    def __init__(self, numThreads, maxSteps, funcSequencer):
        self.numThreads = numThreads
        self.maxSteps = maxSteps
        self.funcSequencer = funcSequencer
        # Internal class variables
        self.dispatcher = WorkDispatcher(self)
        self.curStep = 0
        self.threadList = []
        # self.stepGate = threading.Condition() # Gate to hold/sync all threads
        self.numWaitingThreads = 0

        # Thread coordination
        self.lock = threading.Lock() # for critical section execution
        self.mainGate = threading.Condition()

    # starting to run all the threads, in locking steps
    def run(self):        
        # Create the threads
        for tid in range(0, self.numThreads):
            workerThread = WorkerThread(self, tid)            
            self.threadList.append(workerThread)
            workerThread.start() # start, but should block immediately before step 0

        # Coordinate all threads step by step
        self.curStep = -1 # not started yet
        while(self.curStep < self.maxSteps):
            logger.debug("Main thread going to sleep")
            self.mainGate.acquire()
            self.mainGate.wait() # start snoozing
            self.mainGate.release
            logger.debug("Main thread woke up") # Now not all threads had time to go to sleep
            time.sleep(0.01) # This is like forever 
            
            self.curStep += 1 # starts with 0
            self.tapAllThreads()

        # The threads will run through many steps
        for workerThread in self.threadList:
            workerThread.thread.join() # slight hack, accessing members

        logger.info("All threads finished")

    def reportThreadWaiting(self):
        allThreadWaiting = False
        with self.lock:
            self.numWaitingThreads += 1
            if ( self.numWaitingThreads == self.numThreads ):
                allThreadWaiting = True

        if (allThreadWaiting): # aha, pass the baton to the main thread
            logger.debug("All threads are now waiting")
            self.numWaitingThreads = 0 # do this 1st to avoid race condition
            # time.sleep(0.001) # thread yield, so main thread can be ready
            self.mainGate.acquire()
            self.mainGate.notify() # main thread would now start to run
            self.mainGate.release()
            time.sleep(0) # yield, maybe main thread can run for just a bit

    # def waitForStep(self):
    #     shouldWait = True; 
    #     with self.lock:
    #         # if ( self.numWaitingThreads == 0 ):  # first one here
    #         #    self.stepGate.acquire() # acquire the underlying lock

    #         self.numWaitingThreads += 1
    #         # if ( self.numWaitingThreads < self.numThreads ):
    #             # do nothing, we should wait
    #         if ( self.numWaitingThreads == self.numThreads ):
    #             shouldWait = False # we should now wake up
    #         elif ( self.numWaitingThreads > self.numThreads ):
    #             raise RuntimeError("Corrupt state")

    #     self.stepGate.acquire()
    #     if (shouldWait):
    #         self.stepGate.wait()    
    #     else:
    #         self.numWaitingThreads = 0 # fresh start
    #         self.curStep += 1 # do this before letting all threads loose
    #         print("--> Starting step {}".format(self.curStep), end="\r\n") # before notify_all
    #         # self.stepGate.notify_all()
    #         self.wakeUpAll()
    #     self.stepGate.release()
    #     return self.curStep

    def tapAllThreads(self): # in a deterministic manner
        wakeSeq = []
        for i in range(self.numThreads): # generate a random sequence
            if Dice.throw(2) == 1 :
                wakeSeq.append(i)
            else:
                wakeSeq.insert(0, i)
        logger.info("Waking up threads: {}".format(str(wakeSeq)))
        for i in wakeSeq:
            self.threadList[i].tapStepGate()
            time.sleep(0) # yield

# A queue of continguous POSITIVE integers
class LinearQueue():
    def __init__(self):
        self.firstIndex = 1
        self.lastIndex = 0

    def push(self): # Push to the tail (largest)
        if ( self.firstIndex > self.lastIndex ): # impossible, meaning it's empty
            self.lastIndex = self.firstIndex 
            return self.firstIndex
        # Otherwise we have something
        self.lastIndex += 1
        return self.lastIndex

    def pop(self):
        if ( self.firstIndex > self.lastIndex ): # empty
            return 0 
        index = self.firstIndex
        self.firstIndex += 1
        return index


# State of the database as we believe it to be
class DbState():
    def __init__(self):
        self.tableNumQueue = LinearQueue()
        self.openDbServerConnection()

    def openDbServerConnection(self):
        cfgPath = "../../build/test/cfg"   # was: tdDnodes.getSimCfgPath()
        conn = taos.connect(host="127.0.0.1", config=cfgPath) # TODO: make configurable

        tdSql.init(conn.cursor())
        tdSql.prepare() # Recreate database, etc.
        # tdSql.execute('show databases')

    def closeDbServerConnection(self):
        tdSql.close()
        tdLog.info("Disconnecting from database server")

    def getTableNameToCreate(self):
        tblNum = self.tableNumQueue.push()
        return "table_{}".format(tblNum)

    def getTableNameToDelete(self):
        tblNum = self.tableNumQueue.pop()
        if( tblNum==0 ) :
            return False
        return "table_{}".format(tblNum)

class Task():
    def execute(self):
        raise RuntimeError("Must be overriden by child class")

class CreateTableTask(Task):
    def execute(self):
        tableName = dbState.getTableNameToCreate()
        logger.info("    Creating a table {} ...".format(tableName))
        tdSql.execute("create table {} (ts timestamp, speed int)".format(tableName))

class DropTableTask(Task):
    def execute(self):
        tableName = dbState.getTableNameToDelete()
        if ( not tableName ): # May be "False"
            logger.info("Cannot generate a table to delete, skipping...")
            return
        logger.info("    Dropping a table {} ...".format(tableName))
        tdSql.execute("drop table {}".format(tableName))

class AddDataTask(Task):
    def execute(self):
        logger.info("    Adding some data...")

# Deterministic random number generator
class Dice():
    seeded = False # static, uninitialized

    @classmethod
    def seed(cls, s): # static
        if (cls.seeded):
            raise RuntimeError("Cannot seed the random generator more than once")
        cls.verifyRNG()
        random.seed(s)
        cls.seeded = True  # TODO: protect against multi-threading

    @classmethod
    def verifyRNG(cls): # Verify that the RNG is determinstic
        random.seed(0)
        x1 = random.randrange(0, 1000)
        x2 = random.randrange(0, 1000)
        x3 = random.randrange(0, 1000)
        if ( x1 != 864 or x2!=394 or x3!=776 ):
            raise RuntimeError("System RNG is not deterministic")

    @classmethod
    def throw(cls, max): # get 0 to max-1
        return cls.throwRange(0, max)

    @classmethod
    def throwRange(cls, min, max): # up to max-1
        if ( not cls.seeded ):
            raise RuntimeError("Cannot throw dice before seeding it")
        return random.randrange(min, max)


# Anyone needing to carry out work should simply come here
class WorkDispatcher():
    def __init__(self, pool):
        self.pool = pool
        self.totalNumMethods = 2
        self.tasks = [
            CreateTableTask(),
            DropTableTask(),
            AddDataTask(),
        ]

    def throwDice(self):
        return random.randint(0, len(self.tasks) - 1)

    def doWork(self):
        dice = self.throwDice()
        task = self.tasks[dice]
        task.execute()

if __name__ == "__main__":
    logger = logging.getLogger('myApp')
    logger.setLevel(LOGGING_LEVEL)
    ch = logging.StreamHandler()
    logger.addHandler(ch)

    Dice.seed(0) # initial seeding of dice
    dbState = DbState()
    threadPool = SteppingThreadPool(3, 5, 0) 
    threadPool.run()
    logger.info("Finished running thread pool")
    dbState.closeDbServerConnection()
    
