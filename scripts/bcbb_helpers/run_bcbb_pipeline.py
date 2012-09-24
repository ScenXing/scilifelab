#!/usr/bin/env python
import drmaa
import os
import sys
import glob
import time
import yaml
import subprocess
import copy
import tempfile
import argparse
import csv
import re

import bcbio.solexa.flowcell
import bcbio.solexa.samplesheet
from bcbio.utils import safe_makedir
from bcbio.pipeline.config_loader import load_config
import scilifelab.scripts.bcbb_helpers.report_to_gdocs as report

# The directory where CASAVA has written the demuxed output
CASAVA_OUTPUT_DIR = "Unaligned"
# The analysis script for running the pipeline in parallell mode (on one node)  
PARALLELL_ANALYSIS_SCRIPT="automated_initial_analysis.py"
# The analysis script for running the pipeline in distributed mode (across multiple nodes/cores)
DISTRIBUTED_ANALYSIS_SCRIPT="distributed_nextgen_pipeline.py"
# For non-CASAVA analysis, this script is used to sanitize the run_info.yaml configuration file
PROCESS_YAML_SCRIPT = "process_run_info.py"
# If True, will sanitize the run_info.yaml configuration file when running non-CASAVA analysis
PROCESS_YAML = True
# If True, will assign the distributed master process and workers to a separate RabbitMQ queue for each flowcell 
FC_SPECIFIC_AMPQ = True

def main(post_process_config_file, fc_dir, run_info_file=None, only_run=False, only_setup=False, ignore_casava=False):
    
    run_arguments = [[os.getcwd(),post_process_config_file,fc_dir,run_info_file]]
    if has_casava_output(fc_dir) and not ignore_casava:
        if not only_run:
            run_arguments = setup_analysis_directory_structure(post_process_config_file, fc_dir, run_info_file)
             
    else:
        if not only_run:
            run_arguments = setup_analysis(post_process_config_file, fc_dir, run_info_file)
    
    if not only_setup:
        for arguments in run_arguments:
            run_analysis(*arguments)
 
def run_analysis(work_dir, post_process, fc_dir, run_info):
    """Changes into the supplied work_dir directory and submits 
        the job using the supplied arguments and with slurm parameters
        obtained from the post_process.yaml configuration
    """
    
    # Move to the working directory
    start_dir = os.getcwd()
    os.chdir(work_dir)
    
    config = load_config(post_process)
    
    if str(config["algorithm"]["num_cores"]) == "messaging":
        analysis_script = DISTRIBUTED_ANALYSIS_SCRIPT
    else:
        analysis_script = PARALLELL_ANALYSIS_SCRIPT
        
    job_cl = [analysis_script, post_process, fc_dir, run_info]
    
    cp = config["distributed"]["cluster_platform"]
    cluster = __import__("bcbio.distributed.{0}".format(cp), fromlist=[cp])
    platform_args = config["distributed"]["platform_args"].split()
    
    print "Submitting job"
    jobid = cluster.submit_job(platform_args, job_cl)
    print 'Your job has been submitted with id ' + jobid

    # Change back to the starting directory
    os.chdir(start_dir)

def setup_analysis(post_process_config, archive_dir, run_info_file):
    """Does a non-casava pre-analysis setup and returns a list of arguments
       that can be passed to the run_analysis function in order to start the
       analysis.
    """
    
    # Set the barcode type in run_info.yaml to "illumina", strip the 7th nucleotide and set analysis to 'Minimal'
    if run_info_file is not None and PROCESS_YAML:
        print "---------\nProcessing run_info:"
        run_info_backup = "%s.orig" % run_info_file
        os.rename(run_info_file,run_info_backup)
        cl = ["%s" % PROCESS_YAML_SCRIPT,run_info_backup,"--analysis","Align_illumina","--out_file",run_info_file,"--ascii","--clear_description"]
        print subprocess.check_output(cl)
        print "\n---------\n"
    
    # Check that the specified paths exist
    print "Checking input paths"
    for path in (post_process_config,archive_dir,run_info_file):
        if path is not None and not os.path.exists(path):
            raise Exception("The path %s does not exist" % path)
 
    print "Getting base_dir from %s" % post_process_config
    # Parse the config to get the analysis directory
    with open(post_process_config) as ppc:
        config = yaml.load(ppc)
    
    analysis = config.get("analysis",{})
    base_dir = analysis["base_dir"]
    
    print "Getting run name from %s" % archive_dir
    # Get the run name from the archive dir
    _,run_name = os.path.split(os.path.normpath(archive_dir))

    # Create the working directory if necessary and change into it
    work_dir = os.path.join(base_dir,run_name)
    os.chdir(base_dir)
    print "Creating/changing to %s" % work_dir
    try:
        os.mkdir(run_name,0770)
    except OSError:
        pass
    os.chdir(run_name)
 
    # make sure that the work dir exists
    if not os.path.exists(work_dir):
        raise Exception("The path %s does not exist and was not created" % work_dir)
    
    # if required, parse the machine id and flowcell position and use an ampq vhost specific for it
    if FC_SPECIFIC_AMPQ:
        machine_id = None
        flowcell_position = None
        for p in run_name.upper().split("_"):
            if p.startswith("SN"):
                machine_id = p
            elif p[0] in ("A","B") and p.endswith("XX"):
                flowcell_position = p[0]
        assert machine_id and flowcell_position, "Machine id and flowcell position could not be parsed from run name '%s'" % run_name
        
        # write a dedicated post_process.yaml for the ampq queue
        if config.get("distributed",False):
            config["distributed"]["rabbitmq_vhost"] = "bionextgen-%s-%s" % (machine_id,flowcell_position)
        
        post_process_config_orig = post_process_config
        parts = os.path.splitext(post_process_config)
        post_process_config = "%s-%s-%s%s" % (parts[0],machine_id,flowcell_position,parts[1])
        
        with open(post_process_config,"w") as fh:
            fh.write(yaml.safe_dump(config, default_flow_style=False, allow_unicode=True, width=1000)) 
            
    return [[os.getcwd(),post_process_config,archive_dir,run_info_file]]
        
        
def setup_analysis_directory_structure(post_process_config_file, fc_dir, custom_config_file):
    """Parse the CASAVA 1.8+ generated flowcell directory and create a 
       corresponding directory structure suitable for bcbb analysis,
       complete with sample-specific and project-specific configuration files.
       Returns a list of arguments, both sample- and project-specific, that can 
       be passed to the run_analysis method for execution
    """
    config = load_config(post_process_config_file)
    analysis_dir = os.path.abspath(config["analysis"]["base_dir"])
    assert os.path.exists(fc_dir), "ERROR: Flowcell directory %s does not exist" % fc_dir
    assert os.path.exists(analysis_dir), "ERROR: Analysis top directory %s does not exist" % analysis_dir
    
    # A list with the arguments to each run, when running by sample
    project_run_arguments = []
    sample_run_arguments = []
    
    # Parse the flowcell dir
    fc_dir_structure = parse_casava_directory(fc_dir)
    [fc_date, fc_name] = [fc_dir_structure['fc_date'],fc_dir_structure['fc_name']]
    fc_run_id = "%s_%s" % (fc_date,fc_name)
    
    # Copy the basecall stats directory 
    _copy_basecall_stats(os.path.join(fc_dir_structure['fc_dir'],fc_dir_structure['basecall_stats_dir']), analysis_dir)
    
    # Parse the custom_config_file
    custom_config = []
    if custom_config_file is not None:
        with open(custom_config_file) as fh:
            custom_config = yaml.load(fh)
    
    # Iterate over the projects in the flowcell directory
    for project in fc_dir_structure.get('projects',[]):
        # Create a project directory if it doesn't already exist
        project_name = project['project_name'].replace('__','.')
        project_dir = os.path.join(analysis_dir,project_name)
        if not os.path.exists(project_dir):
            os.mkdir(project_dir,0770)
        
        # Merge the samplesheets of the underlying samples
        src_project_dir = os.path.join(fc_dir_structure['fc_dir'],fc_dir_structure['data_dir'],project['project_dir'])
        samplesheets = []
        for sample in project.get('samples',[]):
            samplesheets.append(os.path.join(src_project_dir,sample['sample_dir'],sample['samplesheet']))            
        project_samplesheet = _merge_samplesheets(samplesheets, os.path.join(project_dir,"SampleSheet.csv"))
        
        # Create a bcbb yaml config file from the samplesheet
        project_config = bcbb_configuration_from_samplesheet(project_samplesheet)
        # Create custom configs for each sample containing the sample files and overload
        for sample in project.get('samples',[]):
            sample_name = sample['sample_name'].replace('__','.')
            custom_sample_cfg = _sample_files_custom_config([os.path.join(project_dir,sample_name,fc_run_id,sf) for sf in sample['sample_files']])
            project_config = override_with_custom_config(project_config,custom_sample_cfg)
                
        project_config = override_with_custom_config(project_config,custom_config)
        arguments = _setup_config_files(project_dir,project_config,post_process_config_file,fc_dir_structure['fc_dir'],project_name,fc_date,fc_name)
        project_run_arguments.append([arguments[1],arguments[0],arguments[1],arguments[3]])
        
        # Iterate over the samples in the project
        for sample_no, sample in enumerate(project.get('samples',[])):
            # Create a directory for the sample if it doesn't already exist
            sample_name = sample['sample_name'].replace('__','.')
            sample_dir = os.path.join(project_dir,sample_name)
            if not os.path.exists(sample_dir):
                os.mkdir(sample_dir,0770)
            
            # Create a directory for the flowcell if it does not exist
            dst_sample_dir = os.path.join(sample_dir,fc_run_id)
            if not os.path.exists(dst_sample_dir):
                os.mkdir(dst_sample_dir,0770)
            
            # rsync the source files to the sample directory
            src_sample_dir = os.path.join(src_project_dir,sample['sample_dir'])
            sample_files = do_rsync([os.path.join(src_sample_dir,f) for f in (sample.get('files',[]) + [sample.get('samplesheet',None)])],dst_sample_dir)
            
            # Generate a sample-specific configuration yaml structure
            samplesheet = os.path.join(src_sample_dir,sample['samplesheet'])
            sample_config = bcbb_configuration_from_samplesheet(samplesheet)
             
            # Append the sequence files to the config
            for lane in sample_config:
                if 'multiplex' in lane:
                    for sample in lane['multiplex']:
                        sample['files'] = [os.path.basename(f) for f in sample_files if f.find("_%s_L00%d_" % (sample['sequence'],int(lane['lane']))) >= 0]
                else:
                    lane['files'] = [os.path.basename(f) for f in sample_files if f.find("_L00%d_" % int(lane['lane'])) >= 0]
            
            # Override the sample config with project-generated ids and custom config
            sample_config = override_with_custom_config(sample_config,project_config)
            
            arguments = _setup_config_files(dst_sample_dir,sample_config,post_process_config_file,fc_dir_structure['fc_dir'],sample_name,fc_date,fc_name)
            sample_run_arguments.append([arguments[1],arguments[0],arguments[1],arguments[3]])
        
    return sample_run_arguments

def create_project_analysis_structure(project, destination_dir, fc_run_id):
    """Create a project's analysis directory structure in the specified destination directory
    """
    assert os.path.exists(destination_dir), \
    "Analysis top-level directory, {}, does not exist".format(destination_dir)
    
    # Create a project directory if it doesn't already exist
    project_name = project['project_name'].replace('__','.')
    project_dir = os.path.join(destination_dir,project_name)
    safe_makedir(project_dir)
    
    # Create the sample directories if they don't already exist
    for sample in project.get('samples',[]):
        sample_name = sample['sample_name'].replace('__','.')
        sample_dir = os.path.join(project_dir,sample_name,fc_run_id)
        safe_makedir(sample_dir)
        
    return project_dir

def copy_project_analysis_files(project, src_project_dir, dest_project_dir, fc_run_id):
    """Copy the project sequence files and samplesheets to the analysis directory
    """
    
    assert os.path.exists(dest_project_dir), \
    "The project analysis directory, {}, does not exists".format(dest_project_dir)
    
    # Loop over the samples and add rsync statements
    rsync_files = []
    for sample in project.get('samples',[]):
        sample_name = sample['sample_name'].replace('__','.')
        src_sample_dir = os.path.join(src_project_dir,sample['sample_dir'])
        dest_sample_dir = os.path.join(dest_project_dir,sample_name,fc_run_id)
        for f in sample.get('files',[]) + [sample.get('samplesheet',None)]:
            if f is not None:
                rsync_files.append([os.path.join(src_sample_dir,f),dest_sample_dir])
    
    # Do the rsync
    for src_file, dst_dir in rsync_files:
        do_rsync([src_file],dst_dir)

def reduce_samplesheet_to_project(samplesheet, project_name, dest_project_dir):
    """Extract the rows for the project from a samplesheet
    """
    data = []
    header = []
    with open(samplesheet) as fh:
        csvread = csv.DictReader(fh, dialect='excel')
        header = csvread.fieldnames
        data = [row for row in csvread if row['SampleProject'] == project_name or row['SampleProject'].replace('__','.') == project_name]
       
    project_samplesheet = os.path.join(dest_project_dir,"{}_{}".format([project_name,os.path.basename(samplesheet)]))         
    with open(project_samplesheet,"w") as outh:
        csvwrite = csv.DictWriter(outh,header)
        csvwrite.writeheader()
        csvwrite.writerows(sorted(data, key=lambda d: (d['Lane'],d['Index'])))
        
    return project_samplesheet
    
    
def _sample_files_custom_config(sample_files):
    """Create a custom sample config to pass the file names
    """
    fields = _parse_sample_filename(sample_files[0])
    
    config = {'lane': fields['Lane']}
    sample_config = {}
    if pcs['Index'] != 'NoIndex':
        sample_config['sequence'] = pcs['Index']
        config['multiplex'] = [sample_config]
    else:
        sample_config = config
    
    sample_config['files'] = sample_files
        
    return config

def _parse_sample_filename(fname):
    """Extract the sample data that can be deduced from the sample filename
    """
    pattern = r"^(.+?)_(NoIndex|Undetermined|[ACGT\-]+)_L(\d+)_R(\d)_(\d+)\.fastq.*$"
    fields = ['SampleID','Index','Lane','Read','Chunk']
    m = re.match(pattern,fname)
    pcs = []
    if m is not None:
        for g in m.groups():
            try:
                p = int(g)
            except:
                p = g
            pcs.append(p)
    if len(pcs) != len(fields):
        raise ValueError('Invalid file name format')
    
    return dict(zip(fields,pcs))

def _merge_samplesheets(samplesheets, merged_samplesheet):
    """Merge several .csv samplesheets into one
    """
    data = []
    header = []
    for samplesheet in samplesheets:
        with open(samplesheet) as fh:
            csvread = csv.DictReader(fh, dialect='excel')
            header = csvread.fieldnames
            for row in csvread:
                data.append(row)
                
    with open(merged_samplesheet,"w") as outh:
        csvwrite = csv.DictWriter(outh,header)
        csvwrite.writeheader()
        csvwrite.writerows(sorted(data, key=lambda d: (d['Lane'],d['Index'])))
        
    return merged_samplesheet
    
def _copy_basecall_stats(source_dir, destination_dir):
    """Copy relevant files from the Basecall_Stats_FCID directory
       to the analysis directory
    """
    
    # First create the directory in the destination
    dirname = os.path.join(destination_dir,os.path.basename(source_dir))
    try:
        os.mkdir(dirname)
    except:
        pass
    
    # List the files/directories to copy
    files = glob.glob(os.path.join(source_dir,"*.htm"))
    files += glob.glob(os.path.join(source_dir,"*.xml"))
    files += glob.glob(os.path.join(source_dir,"*.xsl"))
    files += [os.path.join(source_dir,"Plots")]
    files += [os.path.join(source_dir,"css")]
    do_rsync(files,dirname)
 
def override_with_custom_config(org_config, custom_config):
    """Override the default configuration from the .csv samplesheets
       with a custom configuration. Will replace overlapping options
       or add options that are missing from the samplesheet-generated
       config.
    """
    
    new_config = copy.deepcopy(org_config)
    
    for item in new_config:
        for custom_item in custom_config:
            if item['lane'] != custom_item.get('lane',""):
                continue
            for key, val in custom_item.items():
                if key == 'multiplex':
                    continue
                item[key] = val
                
            for sample in item.get('multiplex',[]):
                if 'sequence' not in sample:
                    continue
                for custom_sample in custom_item.get('multiplex',[]):
                    if sample['sequence'] == custom_sample.get('sequence',""):
                        for key, val in custom_sample.items():
                            sample[key] = val
                        break
            break
        
    return new_config
       
def _setup_config_files(dst_dir,configs,post_process_config_file,fc_dir,sample_name="run",fc_date=None,fc_name=None):
    
    # Setup the data structure
    config_data_structure = {'details': configs}
    if fc_date is not None:
        config_data_structure['fc_date'] = fc_date
    if fc_name is not None:
        config_data_structure['fc_name'] = fc_name
        
    # Dump the config to file
    config_file = os.path.join(dst_dir,"%s-bcbb-config.yaml" % sample_name)
    with open(config_file,'w') as fh:
        fh.write(yaml.safe_dump(config_data_structure, default_flow_style=False, allow_unicode=True, width=1000))
            
    # Copy post-process file
    with open(post_process_config_file) as fh:
        local_post_process = yaml.load(fh) 
    # Update galaxy config to point to the original location
    local_post_process['galaxy_config'] = bcbio.utils.add_full_path(local_post_process['galaxy_config'],os.path.abspath(os.path.dirname(post_process_config_file)))
    # Add job name and output paths to the cluster platform arguments
    if 'distributed' in local_post_process and 'platform_args' in local_post_process['distributed']:
        slurm_out = "%s-bcbb.log" % sample_name
        local_post_process['distributed']['platform_args'] = "%s -J %s -o %s -D %s" % (local_post_process['distributed']['platform_args'], sample_name, slurm_out, dst_dir)
            
    local_post_process_file = os.path.join(dst_dir,"%s-post_process.yaml" % sample_name)
    with open(local_post_process_file,'w') as fh:
        fh.write(yaml.safe_dump(local_post_process, default_flow_style=False, allow_unicode=True, width=1000))
            
    # Write the command for running the pipeline with the configuration files
    run_command_file = os.path.join(dst_dir,"%s-bcbb-command.txt" % sample_name)
    with open(run_command_file,"w") as fh:
        fh.write(" ".join([os.path.basename(__file__),"--only-run","--no-google-report",os.path.basename(local_post_process_file), os.path.join("..",os.path.basename(dst_dir)), os.path.basename(config_file)])) 
        fh.write("\n")   
    
    return [os.path.basename(local_post_process_file), dst_dir, fc_dir, os.path.basename(config_file)]
    
def bcbb_configuration_from_samplesheet(csv_samplesheet):
    """Parse an illumina csv-samplesheet and return a dictionary suitable for the bcbb-pipeline
    """
    tfh, yaml_file = tempfile.mkstemp('.yaml','samplesheet')
    os.close(tfh)
    yaml_file = bcbio.solexa.samplesheet.csv2yaml(csv_samplesheet,yaml_file)
    with open(yaml_file) as fh:
        config = yaml.load(fh)
    
    # Replace the default analysis
    ## TODO: This is an ugly hack, should be replaced by a custom config 
    for lane in config:
        if lane.get('genome_build','') == 'hg19':
            lane['analysis'] = 'Align_standard_seqcap'
        else:
            lane['analysis'] = 'Align_standard'
        for plex in lane.get('multiplex',[]):
            if plex.get('genome_build','') == 'hg19':
                plex['analysis'] = 'Align_standard_seqcap'
            else:
                plex['analysis'] = 'Align_standard'
                
    # Remove the yaml file, we will write a new one later
    os.remove(yaml_file)
    
    return config
                
def do_rsync(src_files, dst_dir):
    cl = ["rsync","-car"]
    cl.extend(src_files)
    cl.append(dst_dir)
    cl = [str(i) for i in cl]
    # For testing, just touch the files rather than copy them
    # for f in src_files:
    #    open(os.path.join(dst_dir,os.path.basename(f)),"w").close()
    subprocess.check_call(cl)
    
    return [os.path.join(dst_dir,os.path.basename(f)) for f in src_files]
        
def parse_casava_directory(fc_dir):
    """Traverse a CASAVA 1.8+ generated directory structure and return a dictionary
    """ 
    projects = []
    
    fc_dir = os.path.abspath(fc_dir)
    fc_name, fc_date = bcbio.solexa.flowcell.get_flowcell_info(fc_dir)
    fc_samplesheet = _get_samplesheet(fc_dir) 
    unaligned_dir = os.path.join(fc_dir,CASAVA_OUTPUT_DIR)
    basecall_stats_dir_pattern = os.path.join(unaligned_dir,"Basecall_Stats_*")
    basecall_stats_dir = None
    try:
        basecall_stats_dir = os.path.relpath(glob.glob(basecall_stats_dir_pattern)[0],fc_dir)
    except:
        print "WARNING: Could not locate basecall stats directory under %s" % unaligned_dir
        
    project_dir_pattern = os.path.join(unaligned_dir,"Project_*")
    for project_dir in glob.glob(project_dir_pattern):
        project_samples = []
        sample_dir_pattern = os.path.join(project_dir,"Sample_*")
        for sample_dir in glob.glob(sample_dir_pattern):
            fastq_file_pattern = os.path.join(sample_dir,"*.fastq.gz")
            samplesheet_pattern = os.path.join(sample_dir,"*.csv")
            fastq_files = [os.path.basename(file) for file in glob.glob(fastq_file_pattern)]
            samplesheet = glob.glob(samplesheet_pattern)
            assert len(samplesheet) == 1, "ERROR: Could not unambiguously locate samplesheet in %s" % sample_dir
            sample_name = sample_dir.replace(sample_dir_pattern[0:-1],'')
            project_samples.append({'sample_dir': os.path.relpath(sample_dir,project_dir), 'sample_name': sample_name, 'files': fastq_files, 'samplesheet': os.path.basename(samplesheet[0])})
        project_name = project_dir.replace(project_dir_pattern[0:-1],'')
        projects.append({'project_dir': os.path.relpath(project_dir,unaligned_dir), 'project_name': project_name, 'samples': project_samples})
    
    return {'fc_dir': fc_dir, 
            'fc_name': fc_name, 
            'fc_date': fc_date, 
            'samplesheet': fc_samplesheet,
            'data_dir': os.path.relpath(unaligned_dir,fc_dir), 
            'basecall_stats_dir': basecall_stats_dir, 
            'projects': projects}
    
def has_casava_output(fc_dir):
    try:
        structure = parse_casava_directory(fc_dir)
        if len(structure['projects']) > 0:
            return True
    except:
        pass
    return False

def _get_samplesheet(flowcell_dir):
    """Get the samplesheet from the flowcell directory, returning firstly [FCID].csv and secondly SampleSheet.csv
    """
    pattern = os.path.join(flowcell_dir,"*.csv")
    ssheet = None
    for f in glob.glob(pattern):
        if not os.path.isfile(f):
            continue
        name, _ = os.path.splitext(os.path.basename(f))
        if flowcell_dir.endswith(name):
            return f
        if name == "SampleSheet":
            ssheet = f
    return ssheet

def report_to_gdocs(fc_dir, post_process_config_file):
    # Rename any existing run_info.yaml as it will interfere with gdocs upload
    run_info = os.path.join(fc_dir, "run_info.yaml")
    if os.path.exists(run_info):
        os.rename(run_info, "{}.bak".format(run_info))
    report.main(os.path.basename(os.path.abspath(fc_dir)), post_process_config_file)

if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Wrapper script for bcbb pipeline. If given a .yaml configuration file, "\
                                     "a run folder containing the sequence data from an illumina run and, optionally, "\
                                     "a custom yaml file with options that should override what is specified in the "\
                                     "config and samplesheet, the script will copy the relevant files from [store_dir] "\
                                     "to [base_dir] and submit the automated_initial_analysis.py pipeline script for each "\
                                     "sample to the cluster platform specified in the configuration.")

    parser.add_argument("config", action="store", default=None, help="Path to the .yaml pipeline configuration file")
    parser.add_argument("fcdir", action="store", default=None, help="Path to the archive run folder")
    parser.add_argument("custom_config", action="store", default=None, help="Path to a custom configuration file with lane or sample specific options that will override the main configuration", nargs="?")
    parser.add_argument("-r", "--only-run", dest="only_run", action="store_true", default=False, help="Don't setup the analysis directory, just start the pipeline")
    parser.add_argument("-s", "--only-setup", dest="only_setup", action="store_true", default=False, help="Setup the analysis directory but don't start the pipeline")
    parser.add_argument("-i", "--ignore-casava", dest="ignore_casava", action="store_true", default=False, help="Ignore any Casava 1.8+ file structure and just assume the pre-casava pipeline setup")
    parser.add_argument("-g", "--no-google-report", dest="no_google_report", action="store_true", default=False, help="Don't upload any demultiplex statistics to Google Docs")
    args = parser.parse_args()
        
    main(args.config,args.fcdir,args.custom_config,args.only_run,args.only_setup,args.ignore_casava)
    if not args.no_google_report:
        report_to_gdocs(args.fcdir, args.config)

# --- Testing code: run with 'nosetests -v -s run_bcbb_pipeline.py'

import unittest
import tempfile
import random
import datetime
import shutil
import string
import mock
import bcbio.utils as utils

import tests.generate_test_data as td

#class CasavaStructureBuilder(unittest.TestCase):
#    """Builder for the Casava file structure
#    """
#    
#    def setUp(self):
#        
#        # Create the file structure in a temporary location
#        self.test_archive_dir = tempfile.mkdtemp()
#        
#        # Create a flowcell id
#        barcode = generate_fc_barcode()
#        fcid = generate_run_id(fc_barcode=barcode)
#        self.fc_dir = os.path.join(self.test_archive_dir,fcid)
#        os.makedirs(self.fc_dir)
#        
#        # Generate run data and write it to a samplesheet
#        self.samplesheet = os.path.join(self.fc_dir,"SampleSheet.csv")
#        generate_run_samplesheet(barcode,self.samplesheet)
#        
#        # Create the file structure according to the samplesheet
#        with open(self.samplesheet) as in_handle:
#            for row in in_handle:
#                if len(row) == 0 or row[0] == "#":
#                    continue
#                csv_data = row.strip().split(",")
#                lane = 0
#                try:
#                    lane = int(str(csv_data[1]))
#                except ValueError:
#                    # This is most likely the header row
#                    continue
#                
#                sample_name = csv_data[2]
#                project_name = csv_data[9]
#                index_sequence = csv_data[4]
#                
#                project_folder = os.path.join(self.fc_dir,"Unaligned","Project_{}".format(project_name))
#                sample_folder = os.path.join(project_folder,"Sample_{}".format(sample_name))
#                sample_file_r1 = os.path.join(sample_folder,"{}_{}_L00{}_R1_001.fastq.gz".format(
#                                                    sample_name,
#                                                    index_sequence,
#                                                    lane))
#                sample_file_r2 = sample_file_r1.replace("_R1_","_R2_")
#                sample_ssheet = os.path.join(sample_folder,"SampleSheet.csv")
#                
#                os.makedirs(sample_folder)
#                utils.touch_file(sample_file_r1)
#                utils.touch_file(sample_file_r2)
#                sample_ssheet = _write_samplesheet([csv_data],sample_ssheet)
#                
#        # Create a Basecall_Stats_[FCID] folder and a Demultiplex_Stats.htm file
#        bcall_dir = os.path.join(self.fc_dir,"Unaligned","Basecall_Stats_{}".format(barcode))
#        os.mkdir(bcall_dir)
#        os.mkdir(os.path.join(bcall_dir,"Plots"))
#        os.mkdir(os.path.join(bcall_dir,"css"))
#        utils.touch_file(os.path.join(bcall_dir,"Demultiplex_Stats.htm"))
#    
#    def tearDown(self):
#        shutil.rmtree(self.test_archive_dir)
#        
#class CasavaStructureTest(CasavaStructureBuilder):
#    
#    def test_has_casava_output_true(self):
#        """Test that has_casava_output returns true
#        """
#        self.assertTrue(has_casava_output(self.fc_dir))
#    
#    def test_has_casava_output_false(self):
#        """Test that has_casava_output returns false
#        """
#        # FIXME: How do we mock the implicit call to parse_casava_directory? The code below does not work..
#        #parse_casava_directory = mock.Mock(return_value={'projects': []})
#        #self.assertFalse(has_casava_output(self.fc_dir))
#        pass
            
class SamplesheetTest(unittest.TestCase):
    
    def tearDown(self):
        shutil.rmtree(self.test_analysis_dir)
        
    def setUp(self):
         
        self.test_analysis_dir = tempfile.mkdtemp()
#        CasavaStructureBuilder.setUp(self)
#        self.post_process_config_file = os.path.join(self.test_archive_dir,"test_post_process.yaml")
#        config = {
#                  'analysis': {
#                               'base_dir': self.test_analysis_dir,
#                               'store_dir': self.test_archive_dir
#                               },
#                  'galaxy_config': 'galaxy',
#                  'distributed': {
#                                  'platform_args': 'slurm'
#                                  }
#                  }
#        
#        with open(self.post_process_config_file,'w') as fh:
#            fh.write(yaml.safe_dump(config, default_flow_style=False, allow_unicode=True, width=1000))
#            
#        setup_analysis_directory_structure(self.post_process_config_file,self.fc_dir,None)
        
        
    def test_project_based_setup(self):
        """Test the project based setup
        """
        self.assertTrue(os.path.exists(self.test_analysis_dir))
    
    def test__merge_samplesheets(self):
        """Test the merging of the samplesheets
        """ 
        src_rows = _parse_samplesheet(generate_run_samplesheet())

        # Write individual samplesheets for each row
        samplesheets = []
        for i in range(1,len(src_rows)):
            fh, tfile = tempfile.mkstemp(dir=self.test_analysis_dir)
            os.close(fh)
            with open(tfile,"w") as outh:
                # Write the header row to each samplesheet
                outh.write("{}\n".format(",".join(src_rows[0])))
                # Write a varying number of rows 
                for j in range(random.randint(1,5)):
                    i += j
                    if i < len(src_rows):
                        outh.write("{}\n".format(",".join(src_rows[i])))
            samplesheets.append(tfile)
        
        # Merge the samplesheets
        fh, tfile = tempfile.mkstemp(dir=self.test_analysis_dir)
        os.close(fh)
        merged_file = _merge_samplesheets(samplesheets,tfile)
        merged_rows = _parse_samplesheet(merged_file)
        for i,item in enumerate(merged_rows[0]):
            self.assertEqual(item,src_rows[0][i])
        src_rows.remove(merged_rows[0])
        
        for i in range(1,len(merged_rows)):
            self.assertTrue(merged_rows[i] in src_rows)
            src_rows.remove(merged_rows[i])
        
        self.assertEqual(len(src_rows),0)
        

    def test__parse_sample_filename(self):
        """Parse a sample file name
        """
        
        # A valid filename
        expected = {'SampleID': td.generate_sample(),
                    'Index': td.generate_barcode(),
                    'Lane': random.randint(1,8),
                    'Read': random.randint(1,2),
                    'Chunk': 1}
        self.assertDictEqual(expected,
                             _parse_sample_filename(td.generate_sample_file(sample_name=expected['SampleID'],
                                                                            barcode=expected['Index'],
                                                                            lane=expected['Lane'],
                                                                            readno=expected['Read'])),
                             "Parsing barcoded sample filename failed")
        
        # A file without barcode
        expected['Index'] = 'NoIndex'
        self.assertDictEqual(expected,
                             _parse_sample_filename(td.generate_sample_file(sample_name=expected['SampleID'],
                                                                            barcode=expected['Index'],
                                                                            lane=expected['Lane'],
                                                                            readno=expected['Read'])),
                             "Parsing non-barcoded sample filename failed")
        
        # A file of undetermined barcode reads
        expected['Index'] = 'Undetermined'
        self.assertDictEqual(expected,
                             _parse_sample_filename(td.generate_sample_file(sample_name=expected['SampleID'],
                                                                            barcode=expected['Index'],
                                                                            lane=expected['Lane'],
                                                                            readno=expected['Read'])),
                             "Parsing undetermined barcode sample filename failed")
        
        # An invalid filename
        with self.assertRaises(ValueError):
            _parse_sample_filename('This_is_an_invalid_filename')
        
      
        
    