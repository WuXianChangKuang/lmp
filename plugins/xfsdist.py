#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# @lint-avoid-python-3-compatibility-imports
#
# xfsdist  Summarize XFS operation latency.
#          For Linux, uses BCC, eBPF.
#
# USAGE: xfsdist [-h] [-T] [-m] [-p PID] [interval] [count]
#
# Copyright 2016 Netflix, Inc.
# Licensed under the Apache License, Version 2.0 (the "License")
#
# 12-Feb-2016   Brendan Gregg   Created this.

from __future__ import print_function
from bcc import BPF
from time import sleep, strftime
import argparse

from init_db import influx_client
from const import DatabaseType
from db_modules import write2db

from datetime import datetime
from time import strftime

# arguments
examples = """examples:
    ./xfsdist            # show operation latency as a histogram
    ./xfsdist -p 181     # trace PID 181 only
    ./xfsdist 1 10       # print 1 second summaries, 10 times
    ./xfsdist -m 5       # 5s summaries, milliseconds
"""
parser = argparse.ArgumentParser(
    description="Summarize XFS operation latency",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
parser.add_argument("-T", "--notimestamp", action="store_true",
    help="don't include timestamp on interval output")
parser.add_argument("-m", "--milliseconds", action="store_true",
    help="output in milliseconds")
parser.add_argument("-p", "--pid",
    help="trace this PID only")
parser.add_argument("interval", nargs="?",
    help="output interval, in seconds")
parser.add_argument("count", nargs="?", default=99999999,
    help="number of outputs")
parser.add_argument("--ebpf", action="store_true",
    help=argparse.SUPPRESS)
args = parser.parse_args()
pid = args.pid
countdown = int(args.count)
if args.milliseconds:
    factor = 1000000
    label = "msecs"
else:
    factor = 1000
    label = "usecs"
if args.interval and int(args.interval) == 0:
    print("ERROR: interval 0. Exiting.")
    exit()
debug = 0

# define BPF program
bpf_text = """
#include <uapi/linux/ptrace.h>
#include <linux/fs.h>
#include <linux/sched.h>

#define OP_NAME_LEN 8
typedef struct dist_key {
    char op[OP_NAME_LEN];
    u64 slot;
    u32 pid
    char comm[TASK_COMM_LEN];
} dist_key_t;
BPF_HASH(start, u32);
BPF_PERF_OUTPUT(events);

// time operation
int trace_entry(struct pt_regs *ctx)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 pid = pid_tgid >> 32;
    u32 tid = (u32)pid_tgid;

    if (FILTER_PID)
        return 0;
    u64 ts = bpf_ktime_get_ns();
    start.update(&tid, &ts);
    return 0;
}

static int trace_return(struct pt_regs *ctx, const char *op)
{
    struct  dist_key data1={};
    u64 *tsp;
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 pid = pid_tgid >> 32;
    u32 tid = (u32)pid_tgid;

    // fetch timestamp and calculate delta
    tsp = start.lookup(&tid);
    if (tsp == 0) {
        return 0;   // missed start or filtered
    }
    u64 delta = (bpf_ktime_get_ns() - *tsp) / FACTOR;

    // store as histogram
    dist_key_t key = {.slot = bpf_log2l(delta)};
    __builtin_memcpy(&key.op, op, sizeof(key.op));
    dist.atomic_increment(key);

    start.delete(&tid);
    data1.pid=pid;
    data1.comm=bpf_get_current_comm(&comm, sizeof(comm));
    data1.solt=key;
    data1.op=op;
    events.perf_submit(ctx, &data, sizeof(data));
    return 0;
}

int trace_read_return(struct pt_regs *ctx)
{
    char *op = "read";
    return trace_return(ctx, op);
}

int trace_write_return(struct pt_regs *ctx)
{
    char *op = "write";
    return trace_return(ctx, op);
}

int trace_open_return(struct pt_regs *ctx)
{
    char *op = "open";
    return trace_return(ctx, op);
}

int trace_fsync_return(struct pt_regs *ctx)
{
    char *op = "fsync";
    return trace_return(ctx, op);
}
"""

# data structure from template
class lmp_data(object):
    def __init__(self,a,b,c,d,e,f):
            self.time = a
            self.glob = b
            self.pid = c
            self.comm = d
            self.op = e
            self.slot = f
            
            
                    
data_struct = {"measurement":'xfsdist',
               "time":[],
               "tags":['glob',],
               "fields":['time','pid','comm','op','slot']}


bpf_text = bpf_text.replace('FACTOR', str(factor))
if args.pid:
    bpf_text = bpf_text.replace('FILTER_PID', 'pid != %s' % pid)
else:
    bpf_text = bpf_text.replace('FILTER_PID', '0')
if debug or args.ebpf:
    print(bpf_text)
    if args.ebpf:
        exit()

# load BPF program
b = BPF(text=bpf_text)

# common file functions
b.attach_kprobe(event="xfs_file_read_iter", fn_name="trace_entry")
b.attach_kprobe(event="xfs_file_write_iter", fn_name="trace_entry")
b.attach_kprobe(event="xfs_file_open", fn_name="trace_entry")
b.attach_kprobe(event="xfs_file_fsync", fn_name="trace_entry")
b.attach_kretprobe(event="xfs_file_read_iter", fn_name="trace_read_return")
b.attach_kretprobe(event="xfs_file_write_iter", fn_name="trace_write_return")
b.attach_kretprobe(event="xfs_file_open", fn_name="trace_open_return")
b.attach_kretprobe(event="xfs_file_fsync", fn_name="trace_fsync_return")

print("Tracing XFS operation latency... Hit Ctrl-C to end.")

# process event
def print_event(cpu, data, size):
    event = b["events"].event(data)
         
    test_data = lmp_data(datetime.now().isoformat(),'glob',event.pid,event.comm.decode('utf-8', 'replace'),event.solt, event.op)
    
    write2db(data_struct, test_data, influx_client, DatabaseType.INFLUXDB.value)
        
        
   # print(("%s Triggered by PID %d (\"%s\"), OOM kill of PID %d (\"%s\")"
   #    ", %d pages, loadavg: %s") % (strftime("%H:%M:%S"), event.fpid,
   #    event.fcomm.decode('utf-8', 'replace'), event.tpid,
   #     event.tcomm.decode('utf-8', 'replace'), event.pages, avgline))

# initialize BPF
b = BPF(text=bpf_text)

b["events"].open_perf_buffer(print_event)
while 1:
    try:
        b.perf_buffer_poll()
    except KeyboardInterrupt:
        exit()