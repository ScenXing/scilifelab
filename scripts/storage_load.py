"""Generates dictionaries for the load on the available file systems
using 'df'. If a couchdb is specified, the dictionaries will be sent there.
Otherwise prints the dictionaries.
"""
import argparse
import datetime
import subprocess
import couchdb
from platform import node as host_name

def main():
    parser = argparse.ArgumentParser(description="Formats file system \
        information as a dict, and sends it to a given CouchDB.")

    parser.add_argument("--server", dest="server", action="store", default="", \
        help="Address to the CouchDB server.")

    parser.add_argument("--db", dest="db", action="store", \
        help="Name of the CouchDB database")

    args = parser.parse_args()

    current_time = datetime.datetime.now()
    df = subprocess.Popen(["df"], stdout=subprocess.PIPE)
    output = df.communicate()[0]
    out_it = iter(output.split("\n"))

    file_systems = []

    keys = out_it.next().split()
    for values in out_it:
        values = values.split()
        for i, v in enumerate(values):
            try:
                values[i] = int(v)
            except:
                pass

        log_dict = dict(zip(keys, values))
        log_dict["time"] = current_time.isoformat()
        log_dict["hostname"] = host_name()

        file_systems.append(log_dict)

    if args.server == "":
        print(file_systems)
    else:
        couch = couchdb.Server(args.server)
        db = couch[args.db]
        for fs_dict in file_systems:
            db.save(fs_dict)

if __name__ == "__main__":
    main()
