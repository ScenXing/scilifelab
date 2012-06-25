
import os

from optparse import OptionParser
from bcbio.solexa.flowcell import get_flowcell_info
from bcbio.pipeline.config_loader import load_config
from bcbio.google.sequencing_report import create_report_on_gdocs

def main(run_id, config_file, archive_dir, analysis_dir, run_info_file=None, dryrun=False):
    
    archive_dir = os.path.normpath(archive_dir)
    analysis_dir = os.path.normpath(analysis_dir)
    
    dirs = {"work": os.path.join(analysis_dir,run_id), 
            "flowcell": os.path.join(archive_dir,run_id)}
    
    if run_info_file is None:
        run_info_file = os.path.join(dirs["flowcell"],"run_info.yaml")
    
    assert run_id, "No run id was specified"
    assert os.path.exists(config_file), "The post process configuration file, %s, could not be found" % config_file
    config = load_config(config_file)
    assert "gdocs_upload" in config, "The configuration file, %s, has no section specifying the Google docs details" % config_file
    assert os.path.exists(run_info_file), "The run info configuration file, %s, could not be found" % run_info_file
    assert os.path.exists(dirs["flowcell"]), "The flowcell directory, %s, could not be found" % dirs["flowcell"]
    assert os.path.exists(dirs["work"]), "The work directory, %s, could not be found" % dirs["work"]    
    
    fc_name, fc_date = get_flowcell_info(dirs["flowcell"])
    
    print "A report will be created on Google Docs based on the demultiplexed data in %s" % dirs["work"]
    print "The configuration file is %s and the run info file is %s" % (config_file,run_info_file)
    print "The run was started on %s and has flowcell id %s" % (fc_date,fc_name)
    
    if not dryrun:
        create_report_on_gdocs(fc_date,fc_name,run_info_file,dirs,config)
    else:
        print "DRY-RUN: nothing uploaded"
    

if __name__ == "__main__":
    parser = OptionParser()
    parser.add_option("-c", "--config-file", dest="config_file", default=None)
    parser.add_option("-r", "--archive-dir", dest="archive_dir", default="/proj/a2010002/archive")
    parser.add_option("-a", "--analysis-dir", dest="analysis_dir", default="/proj/a2010002/nobackup/illumina")
    parser.add_option("-f", "--run-info-file", dest="run_info_file", default=None)
    parser.add_option("-n", "--dry-run", dest="dryrun", action="store_true", default=False)
    options, args = parser.parse_args()
    
    run_id = None
    if len(args) == 1:
        run_id = args[0]
    else:
        print __doc__
        sys.exit()
    main(run_id, options.config_file,
         os.path.normpath(options.archive_dir), os.path.normpath(options.analysis_dir),
         options.run_info_file, options.dryrun)
