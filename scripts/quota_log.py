"""Generates dictionaries for the load on the quota
using 'uquota'. If a couchdb is specified, the dictionaries will be sent there.
Otherwise prints the dictionaries.
"""
import argparse
import datetime
import subprocess
from platform import node as host_name
from pprint import pprint
import couchdb

def main():
    parser = argparse.ArgumentParser(description="Formats uquota \
        information as a dict, and sends it to a given CouchDB.")

    parser.add_argument("--server", dest="server", action="store", default="", \
        help="Address to the CouchDB server.")

    parser.add_argument("--db", dest="db", action="store", \
        help="Name of the CouchDB database")

    args = parser.parse_args()

    current_time = datetime.datetime.now()
    uq = subprocess.Popen(["/bubo/sw/uppmax/bin/uquota", "-q"], stdout=subprocess.PIPE)
    output = uq.communicate()[0]

    projects = output.split("\n/proj/")[1:]

    project_list = []
    for proj in projects:
        project_dict = {"time": current_time.isoformat()}

        project = proj.strip("\n").split()
        project_dict["project"] = project[0]
        project_dict["usage (GB)"] = project[1]
        project_dict["quota limit (GB)"] = project[2]
        try:
            project_dict["over quota"] = project[3]
        except:
            pass

        project_list.append(project_dict)

    if args.server == "":
        pprint(project_list)
    else:
        couch = couchdb.Server(args.server)
        db = couch[args.db]
        for fs_dict in project_list:
            db.save(fs_dict)

if __name__ == "__main__":
    main()
