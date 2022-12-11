#!/usr/bin/python

import argparse
import datetime as dt
import json
import logging
import multiprocessing as mp
import os
import pgworkload.utils.simplefaker
import pgworkload.utils.util
import psycopg
import queue
import random
import re
import signal
import sys
import threading
import time
import traceback
import yaml

DEFAULT_SLEEP = 5


def signal_handler(sig, frame):
    """Handles Ctrl+C events gracefully, 
    ensuring all running processes are closed rather than killed.

    Args:
        sig (_type_): 
        frame (_type_): 
    """
    global stats
    global concurrency
    logging.info("KeyboardInterrupt signal detected. Stopping processes...")

    # send the poison pill to each worker
    for _ in range(concurrency):
        kill_q.put(None)

    # wait until all workers return
    start = time.time()
    c = 0
    timeout = True
    while c < concurrency and timeout:
        try:
            kill_q2.get(block=False)
            c += 1
        except:
            pass

        time.sleep(0.01)
        timeout = time.time() < start + 5

    if not timeout:
        logging.info("Timeout reached - forcing processes to stop")

    logging.info("Printing final stats")
    stats.print_stats()
    sys.exit(0)


def init_pgworkload(args: argparse.Namespace):
    """Performs pgworkload initialization steps

    Args:
        args (argparse.Namespace): args passed at the CLI

    Returns:
        argparse.Namespace: updated args
    """
    logging.debug("Initialazing pgworkload")

    if not args.procs:
        args.procs = os.cpu_count()

    if not re.search(r'.*://.*/(.*)\?', args.dburl):
        logging.error(
            "The connection string needs to point to a database. Example: postgres://root@localhost:26257/postgres?sslmode=disable")
        sys.exit(1)

    if not args.workload_path:
        logging.error("No workload argument was passed")
        print()
        args.parser.print_help()
        sys.exit(1)

    workload = pgworkload.utils.util.import_class_at_runtime(path=args.workload_path)

    args.dburl = pgworkload.utils.util.set_query_parameter(url=args.dburl, param_name="application_name",
                                                     param_value=args.app_name if args.app_name else workload.__name__)

    logging.info(f"URL: '{args.dburl}'")

    # load args dict from file or string
    if os.path.exists(args.args):
        with open(args.args, 'r') as f:
            args.args = f.read()
            # parse into JSON if it's a JSON string
            try:
                args.args = json.load(args.args)
            except Exception as e:
                pass
    else:
        args.args = yaml.safe_load(args.args)
        if isinstance(args.args, str):
            logging.error(
                f"The value passed to '--args' is not a valid JSON or a valid path to a JSON/YAML file: '{args.args}'")
            sys.exit(1)

    return args


def run(args: argparse.Namespace):
    """Run the workload

    Args:
        args (argparse.Namespace): the args passed at the CLI
    """

    global stats
    args = init_pgworkload(args)

    global concurrency

    concurrency = int(args.concurrency)

    workload = pgworkload.utils.util.import_class_at_runtime(path=args.workload_path)

    signal.signal(signal.SIGINT, signal_handler)

    stats = pgworkload.utils.util.Stats(
        frequency=args.frequency, prom_port=args.prom_port)

    if args.iterations > 0:
        args.iterations = int(args.iterations / concurrency)

    global kill_q
    global kill_q2

    q = mp.Queue(maxsize=1000)
    kill_q = mp.Queue()
    kill_q2 = mp.Queue()

    c = 0

    threads_per_proc = pgworkload.utils.util.get_threads_per_proc(
        args.procs, args.concurrency)

    ramp_intervals = int(args.ramp / len(threads_per_proc))

    for i, x in enumerate(threads_per_proc):
        mp.Process(target=worker, daemon=True, args=(
            x-1, q, kill_q, kill_q2, args.dburl, args.autocommit, workload, args.args, args.iterations, args.duration, args.conn_duration)).start()

        if i < len(threads_per_proc)-1:
            time.sleep(ramp_intervals)

    try:
        stat_time = time.time() + args.frequency
        while True:
            try:
                # read from the queue for stats or completion messages
                tup = q.get(block=False)
                if isinstance(tup, tuple):
                    stats.add_latency_measurement(*tup)
                else:
                    c += 1
            except queue.Empty:
                pass

            if c >= concurrency:
                if isinstance(tup, psycopg.errors.UndefinedTable):
                    logging.error(tup)
                    logging.error(
                        "The schema is not present. Did you initialize the workload?")
                    sys.exit(1)
                elif isinstance(tup, Exception):
                    logging.error("Exception raised: %s" % tup)
                    sys.exit(1)
                else:
                    logging.info(
                        "Requested iteration/duration limit reached. Printing final stats")
                    stats.print_stats()
                    sys.exit(0)

            if time.time() >= stat_time:
                stats.print_stats()
                stats.new_window()
                stat_time = time.time() + args.frequency

    except Exception as e:
        logging.error(traceback.format_exc())


def worker(thread_count: int, q: mp.Queue, kill_q: mp.Queue, kill_q2: mp.Queue,
           dburl: str, autocommit: bool,
           workload: object, args: dict, iterations: int, duration: int, conn_duration: int,
           threads: list = []):
    """Process worker function to run the workload in a multiprocessing env

    Args:
        thread_count(int): The number of threads to create
        q (mp.Queue): queue to report query metrics
        kill_q (mp.Queue): queue to handle stopping the worker
        kill_q2 (mp.Queue): queue to handle stopping the worker
        dburl (str): connection string to the database
        autocommit (bool): whether to set autocommit for the connection
        workload (object): workload class object
        args (dict): args to init the workload class
        iterations (int): count of workload iteration before returning
        duration (int): seconds before returning
        conn_duration (int): seconds before restarting the database connection
        threads (list): the list of threads to wait to finish before returning
    """
    threads: list[threading.Thread] = []

    for _ in range(thread_count):
        thread = threading.Thread(
            target=worker,
            daemon=True, args=(0,
                               q, kill_q, kill_q2, dburl, autocommit,
                               workload, args, iterations,
                               duration, conn_duration, [])
        )
        thread.start()
        threads.append(thread)

    if threading.current_thread().name == 'MainThread':
        logging.debug("Process Worker created")
        # capture KeyboardInterrupt and do nothing
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    else:
        logging.debug("Thread Worker created")

    # catch exception while instantiating the workload class
    try:
        w = workload(args)
    except Exception as e:
        stack_lines = traceback.format_exc()
        q.put(Exception(stack_lines))
        return

    c = 0
    endtime = 0
    conn_endtime = 0

    if duration > 0:
        endtime = time.time() + duration

    while True:
        if conn_duration > 0:
            # reconnect every conn_duration +/-10%
            conn_endtime = time.time() + int(conn_duration * random.uniform(.9, 1.1))
        # listen for termination messages (poison pill)
        try:
            kill_q.get(block=False)
            logging.debug("Poison pill received")
            kill_q2.put(None)
            for x in threads:
                x.join()

            return
        except queue.Empty:
            pass
        try:
            with psycopg.connect(dburl, autocommit=autocommit) as conn:
                logging.debug("Connection started")
                while True:

                    # listen for termination messages (poison pill)
                    try:
                        kill_q.get(block=False)
                        logging.debug("Poison pill received")
                        kill_q2.put(None)
                        for x in threads:
                            x.join()
                        return
                    except queue.Empty:
                        pass

                    # return if the limits of either iteration count and duration have been reached
                    if (iterations > 0 and c >= iterations) or \
                            (duration > 0 and time.time() >= endtime):
                        logging.debug("Task completed!")

                        # send task completed notification (a None)
                        q.put(None)
                        for x in threads:
                            x.join()
                        return

                    # break from the inner loop if limit for connection duration has been reached
                    # this will cause for the outer loop to reset the timer and restart with a new conn
                    if conn_duration > 0 and time.time() >= conn_endtime:
                        logging.debug(
                            "conn_duration reached, will reset the connection.")
                        break

                    cycle_start = time.time()
                    for txn in w.run():
                        start = time.time()
                        pgworkload.utils.util.run_transaction(
                            conn, lambda conn: txn(conn))
                        if not q.full():
                            q.put((txn.__name__, time.time() - start))

                    c += 1
                    if not q.full():
                        q.put(('__cycle__', time.time() - cycle_start))

        # catch any error, pass that error to the MainProcess
        except psycopg.errors.UndefinedTable as e:
            q.put(e)
            return
        # psycopg.OperationalErrors can either mean a disconnection
        # or some other errors.
        # We don't stop if a node goes doesn, instead, wait few seconds and attempt
        # a new connection.
        # If the error is not beacuse of a disconnection, then unfortunately
        # the worker will continue forever
        except psycopg.Error as e:
            logging.error(f'{e.__class__.__name__} {e}')
            logging.info("Sleeping for %s seconds" % (DEFAULT_SLEEP))
            time.sleep(DEFAULT_SLEEP)
        except Exception as e:
            logging.error("Exception: %s" % (e))
            q.put(e)
            return

