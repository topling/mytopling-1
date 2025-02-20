import time
import sys
import MySQLdb
from MySQLdb.constants import *
import argparse
import random
import threading
import traceback
import enum

NUM_WORKERS = 50
NUM_TRANSACTIONS = 2000

class Workload(enum.Enum):
    User = 1
    AdminChecks = 2
    Read = 3
    ThreadPoolOnOff = 4

def parse_args():
    parser = argparse.ArgumentParser(
        'multi_query',
        description='Generate Multi query load',
    )
    parser.add_argument(
        '--host',
        type=str,
        help='host name')
    parser.add_argument(
        '--port',
        type=int,
        help='port number')
    parser.add_argument(
        '--user',
        type=str,
        help='user name')
    parser.add_argument(
        '--admin',
        type=str,
        help='admin user name')
    parser.add_argument(
        '--password',
        default='',
        type=str,
        help='password')
    parser.add_argument(
        '--database',
        type=str,
        help='database to use')
    parser.add_argument(
        '--weighted-queues',
        action="store_true",
        help='use weighted queues')
    parser.add_argument(
        '--wait-events',
        action="store_true",
        help='add wait event workload')
    parser.add_argument(
        '--thread-pool-on-off',
        action="store_true",
        help='add concurrent enabling/disabling of thread pool')

    return parser.parse_args()

def is_deadlock_error(exc):
    error_code = exc.args[0]
    return (error_code == MySQLdb.constants.ER.LOCK_DEADLOCK or
            error_code == MySQLdb.constants.ER.LOCK_WAIT_TIMEOUT)

def generate_load(args, worker_id):
    con = MySQLdb.connect(user=args.user,
                          passwd=args.password,
                          host=args.host,
                          port=args.port,
                          db=args.database)

    if args.weighted_queues:
        queue_id = worker_id % 2
        print("WORKER %d: Using queue %d" % (worker_id, queue_id))
        cursor = con.cursor()
        cursor.execute('set thread_pool_admission_control_queue = %d;' % \
            queue_id)
        cursor.close()

    op = 1
    for i in range(NUM_TRANSACTIONS):
        try:
            if args.wait_events:
                op = random.randint(1, 3)
            if op == 1:
                print("WORKER %d: Executing iteration %d" % (worker_id, i))
                cursor = con.cursor()
                cursor.execute('begin;')
                values = []
                for j in range(3):
                    val = random.randrange(1, 10000)
                    values.append(val)
                values = sorted(values)
                for val in values:
                    insert_sql = 'insert into t1 values(%d, 1) on duplicate ' \
                    'key update b=greatest(b+1, 0);' % (val)
                    cursor.execute(insert_sql)
                cursor.execute("commit;")
                cursor.close()
            elif op == 2:
                print("WORKER %d: Executing GET_LOCK %d" % (worker_id, i))
                cursor = con.cursor()
                cursor.execute("select get_lock('testlock', -1)")
                cursor.execute("select release_lock('testlock')")
                cursor.close()
            else:
                print("WORKER %d: Executing SLEEP %d" % (worker_id, i))
                cursor = con.cursor()
                cursor.execute("select sleep(0.1)")
                cursor.close()

            if args.thread_pool_on_off:
                con.close()
                con = MySQLdb.connect(user=args.user,
                                    passwd=args.password,
                                    host=args.host,
                                    port=args.port,
                                    db=args.database)

        except (MySQLdb.OperationalError, MySQLdb.InternalError) as e:
            if not is_deadlock_error(e):
                raise e

    con.close()

def run_reads(args):
    con = MySQLdb.connect(user=args.user,
                          passwd=args.password,
                          host=args.host,
                          port=args.port,
                          db=args.database)
    for i in range(int(NUM_TRANSACTIONS / 10)):
        cursor = con.cursor()
        cursor.execute("select * from t1")
        cursor.execute("select repeat('X', @@max_allowed_packet)")
        cursor.execute("commit")
        cursor.close()

def run_admin_checks(args):
    con = MySQLdb.connect(user=args.user,
                          passwd=args.password,
                          host=args.host,
                          port=args.port,
                          db=args.database)
    cursor=con.cursor()
    cursor.execute("select @@global.thread_pool_max_running_queries")
    rows = cursor.fetchone()
    max_running_queries = int(rows[0])
    for i in range(NUM_TRANSACTIONS):
        cursor=con.cursor()
        cursor.execute("show status like " \
            "'Thread_pool_admission_control_running_queries'")
        rows = cursor.fetchall()
        if int(rows[0][1]) > max_running_queries:
            raise Exception('Current running queries %s is more than ' \
                            'max_running_queries %d' % (rows[1][1],
                             max_running_queries))

def run_tp_on_off(worker):
    con = MySQLdb.connect(user=worker.args.admin,
                          passwd=worker.args.password,
                          host=worker.args.host,
                          port=worker.args.port,
                          db=worker.args.database)
    while not worker.stop:
        print("TP ON/OFF: set global thread_pool_on = 0")
        cursor=con.cursor()
        cursor.execute("set global thread_pool_on = 0")
        cursor.close()
        time.sleep(0.01)

        print("TP ON/OFF: set global thread_pool_on = 1")
        cursor=con.cursor()
        cursor.execute("set global thread_pool_on = 1")
        cursor.close()
        time.sleep(0.01)

    con.close()

class worker_thread(threading.Thread):
    def __init__(self, args, worker_id, workload):
        threading.Thread.__init__(self)
        self.args = args
        self.exception = None
        self.workload = workload
        self.worker_id = worker_id
        self.stop = False
        self.start()

    def run(self):
        try:
            if self.workload == Workload.AdminChecks:
                run_admin_checks(self.args)
            elif self.workload == Workload.Read:
                run_reads(self.args)
            elif self.workload == Workload.ThreadPoolOnOff:
                run_tp_on_off(self)
            else:
                generate_load(self.args, self.worker_id)
        except Exception as e:
            self.exception = traceback.format_exc()

def main():
    args = parse_args()
    workers = []
    for i in range(NUM_WORKERS):
        worker = worker_thread(args, i, Workload.User)
        workers.append(worker)

    admin_worker = worker_thread(args, NUM_WORKERS, Workload.AdminChecks)
    workers.append(admin_worker)

    readonly_worker = worker_thread(args, NUM_WORKERS, Workload.Read)
    workers.append(readonly_worker)

    if args.thread_pool_on_off:
        tp_worker = worker_thread(args, NUM_WORKERS, Workload.ThreadPoolOnOff)

    worker_failed = False
    for w in workers:
        w.join()
        if w.exception:
            print("worker hit an exception:\n%s\n'" % w.exception)
            worker_failed = True

    if args.thread_pool_on_off:
        tp_worker.stop = True
        tp_worker.join()
        if tp_worker.exception:
            print("tp_worker hit an exception:\n%s\n'" % tp_worker.exception)
            worker_failed = True

    if worker_failed:
        sys.exit(1)

if __name__ == '__main__':
  main()
