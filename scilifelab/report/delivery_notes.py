"""Module delivery_notes - code for generating delivery reports and notes"""
import os
import re
import itertools
import ast
import json
import math
import csv
import yaml
import operator
import texttable
from cStringIO import StringIO
from collections import Counter
from scilifelab.db.statusdb import SampleRunMetricsConnection, ProjectSummaryConnection, FlowcellRunMetricsConnection, calc_avg_qv
from scilifelab.utils.misc import query_ok, query_yes_no
from scilifelab.report import sequencing_success
from scilifelab.report.rst import make_sample_rest_notes, make_rest_note
from scilifelab.report.rl import make_note, concatenate_notes, sample_note_paragraphs, sample_note_headers, project_note_paragraphs, project_note_headers, make_sample_table
import scilifelab.log

LOG = scilifelab.log.minimal_logger(__name__)

# Software versions used in data production. Instrument specific?
software_versions = {
    'basecall_software': 'RTA',
    'demultiplex_software' : 'bcl2fastq',
    }

def _parse_instrument_config(cfile):
    """Parse a supplied yaml file with instrument ids and associated metadata and return a list of dicts
    """
    if cfile is None or not os.path.exists(cfile):
        LOG.warn("No instrument config file supplied, will use default value")
        return [{'instrument_id': 'default', 'instrument_alias': 'NN', 'instrument_version': 'NN'}]

    with open(cfile) as fh:
        return yaml.load(fh)

# http://stackoverflow.com/questions/3154460/python-human-readable-large-numbers
def _round_read_count_in_millions(n):
    """Round absolute read counts to million reads"""
    LOG.debug("Rounding read count: got {}".format(n))
    if n is None:
        return None
    if n == 0:
        return 0
    round_factor = [2,2,1]
    millidx = max(0, min(len(round_factor) - 1, int(math.floor(math.log10(abs(int(n)))/3.0))))
    return round(float(n)/10**(6),round_factor[millidx])

def _get_ordered_million_reads(sample_name, ordered_million_reads):
    """Retrieve ordered million reads for sample

    :param sample_name: sample name (possibly barcode name)
    :param ordered_million_reads: parsed option passed to application

    :returns: ordered number of reads or None"""
    if isinstance(ordered_million_reads, dict):
        if sample_name in ordered_million_reads:
            return ordered_million_reads[sample_name]
        else:
            return ordered_million_reads.get("default", -1)
    else:
        return ordered_million_reads

def _get_phix_error_rate(lane, phix):
    """Set phix error rate for a sample based on lane

    :param lane: lane
    :param phix: parsed option passed to application

    :returns: phix error rate or None"""
    if isinstance(phix, dict):
        if int(lane) in phix:
            return phix[int(lane)]
        else:
            return -1
    else:
        return phix

def _get_bc_count(sample_name, bc_count, sample_run):
    """Retrieve barcode count for a sample

    :param sample_name: sample name
    :param bc_count: parsed option passed to application
    :param sample_run: sample run object

    :returns: barcode count or None"""
    if isinstance(bc_count, dict):
        if sample_name in bc_count:
            return bc_count[sample_name]
        else:
            return bc_count.get("default", sample_run.get("bc_count", -1))
    else:
        return bc_count


def _assert_flowcell_format(flowcell):
    """Assert name of flowcell: "[A-Z0-9\-]+"

    :param flowcell: flowcell id

    :returns: boolean
    """
    if flowcell is None:
        # Can this really be right?!?
        return True
    if not re.match("[A-Z0-9\-]+$", flowcell):
        return False
    return True

def _set_sample_run_list(project_name, flowcell, project_alias, s_con):
    """Set sample run list.

    :param project_name: project name
    :param flowcell: flowcell id
    :param project_alias: project alias argument passed to pm
    :param s_con: sample run connection

    :returns: sample_run_list
    """
    sample_run_list = s_con.get_samples(sample_prj=project_name, fc_id=flowcell)
    if not project_alias:
        return sample_run_list
    project_alias = ast.literal_eval(project_alias)
    for p_alias in project_alias:
        sample_run_list_tmp = s_con.get_samples(sample_prj=p_alias, fc_id=flowcell)
        if sample_run_list_tmp:
            sample_run_list.extend(sample_run_list_tmp)
    return sample_run_list

def _literal_eval_option(option, default=None):
    """Literally evaluate passed option.

    :param option: option passed to pm, which could be a file name
    :param default: default value of option

    :returns: parsed option
    """
    if not option:
        return default
    if os.path.exists(option):
        with open(option) as fh:
            option = json.load(fh)
    else:
        option = ast.literal_eval(option)
    return option

def _update_sample_output_data(output_data, cutoffs):
    """Update sample output data dictionary.

    :param output_data: output data dictionary
    :param cutoffs: cutoffs dictionary

    :returns: updated output data dictionary
    """
    output_data["stdout"].write("\nQuality stats\n")
    output_data["stdout"].write("************************\n")
    output_data["stdout"].write("PhiX error cutoff: > {:3}\n".format(cutoffs['phix_err_cutoff']))
    output_data["stdout"].write("QV cutoff        : < {:3}\n".format(cutoffs['qv_cutoff']))
    output_data["stdout"].write("************************\n\n")
    output_data["stdout"].write("{:>18}\t{:>6}\t{:>12}\t{:>12}\t{:>12}\t{:>12}\n".format("Scilifelab ID", "Lane", "PhiXError", "ErrorStatus", "AvgQV", "QVStatus"))
    output_data["stdout"].write("{:>18}\t{:>6}\t{:>12}\t{:>12}\t{:>12}\t{:>12}\n".format("=============", "====", "=========", "===========", "=====", "========"))
    return output_data

def _set_project_sample_dict(project_sample_item, source):
    """Set a project sample dict, mapping a project sample to sample run metrics if present in project summary.

    :param project_sample_item: a project sample item

    :returns: project_sample_d or empty dict
    """
    project_sample_d = {}

    #The structure of the database has changed for projects opened after July 1st
    #2013 (document 10294_01 for more details)
    if source == 'lims':
        LOG.debug("This project has LIMS as source of information")
        if "library_prep" in project_sample_item.keys():
            sample_run_metrics = {k:v.get("sample_run_metrics", {}) for k,v in \
                                    project_sample_item["library_prep"].iteritems()}
            project_sample_d = {}
            for fc in sample_run_metrics.items():
                fc, metrics = fc
                for k, v in metrics.iteritems():
                    sample_run_metrics = v.get('sample_run_metrics_id', '')
                    if sample_run_metrics:
                        project_sample_d[k] = v['sample_run_metrics_id']
                    else:
                        LOG.warn("No sample_run_metrics information for sample '{}'".format(project_sample_item))
        else:
            sample_run_metrics = project_sample_item.get("sample_run_metrics", {})
            project_sample_d = {metrics[0]:metrics[1]['sample_run_metrics_id'] \
                                    for metrics in sample_run_metrics.items()}
            if not project_sample_item.get("sample_run_metrics", {}):
                LOG.warn("No sample_run_metrics information for sample '{}'".format(project_sample_item))
    else:
        if "library_prep" in project_sample_item.keys():
            project_sample_d = {x:y for d in [v.get("sample_run_metrics", {}) \
                    for k,v in project_sample_item["library_prep"].iteritems()] \
                        for x,y in d.iteritems()}
        else:
            project_sample_d = {x:y for x,y in project_sample_item.get("sample_run_metrics", {}).iteritems()}
            if not project_sample_item.get("sample_run_metrics", {}):
                LOG.warn("No sample_run_metrics information for sample '{}'".format(project_sample_item))
    return project_sample_d


def sample_status_note(project_name=None, flowcell=None, username=None, password=None, url=None,
                       ordered_million_reads=None, uppnex_id=None, customer_reference=None, bc_count=None,
                       project_alias=[], projectdb="projects", samplesdb="samples", flowcelldb="flowcells",
                       phix=None, is_paired=True, config=None, **kw):
    """Make a sample status note. Used keywords:

    :param project_name: project name
    :param flowcell: flowcell id
    :param username: db username
    :param password: db password
    :param url: db url
    :param ordered_million_reads: number of ordered reads in millions
    :param uppnex_id: the uppnex id
    :param customer_reference: customer project name
    :param project_alias: project alias name
    :param phix: phix error rate
    :param is_paired: True if run is paired-end, False for single-end
    """
    # Cutoffs
    cutoffs = {
        "phix_err_cutoff" : float(config.get("qc","phix_error_rate_threshold")),
        "qv_cutoff" : 30,
        }

    instrument = _parse_instrument_config(os.path.expanduser(kw.get("instrument_config","")))
    instrument_dict = {i['instrument_id']: i for i in instrument}

    output_data = {'stdout':StringIO(), 'stderr':StringIO(), 'debug':StringIO()}
    if not _assert_flowcell_format(flowcell):
        LOG.warn("Wrong flowcell format {}; skipping. Please use the flowcell id (format \"[A-Z0-9\-]+\")".format(flowcell) )
        return output_data
    output_data = _update_sample_output_data(output_data, cutoffs)

    # Set options
    bc_count = _literal_eval_option(bc_count)
    phix = _literal_eval_option(phix)
    
    # Connect and run
    s_con = SampleRunMetricsConnection(dbname=samplesdb, username=username, password=password, url=url)
    fc_con = FlowcellRunMetricsConnection(dbname=flowcelldb, username=username, password=password, url=url)
    p_con = ProjectSummaryConnection(dbname=projectdb, username=username, password=password, url=url)

    # Get project
    project = p_con.get_entry(project_name)
    source = p_con.get_info_source(project_name)
    if not project:
        LOG.warn("No such project '{}'".format(project_name))
        return output_data

    # Get flowcell
    fc_doc = fc_con.get_flowcell_by_id(flowcell)
    if not fc_doc:
        LOG.warn("No such flowcell '{}'".format(flowcell))
        return output_data
    fc = fc_doc.get("name")

    # Get a dict with the flowcell level information
    fc_param = _get_flowcell_info(fc_con,fc,project_name)
    # Update the dict with project information
    fc_param.update(_get_project_info(project))
    # Update the dict with the relevant QC thresholds
    fc_param.update(_get_qc_thresholds(fc_param,config))
    
    fc_param["pdffile"] = "{}_{}_flowcell_summary.pdf".format(project_name, fc)
    fc_param["rstfile"] = "{}.rst".format(os.path.splitext(fc_param["pdffile"])[0])
    
    # FIXME: parse this from configs
    fc_param.update(software_versions)
    demux_software = fc_con.get_demultiplex_software(fc)
    fc_param["basecaller_version"] = demux_software.get(fc_param["basecall_software"],None)
    fc_param["demultiplex_version"] = demux_software.get(fc_param["demultiplex_software"],"1.8.3")
    
    # Get uppnex id, possible overridden on the command line
    if uppnex_id:
        fc_param["uppnex_project_id"] = uppnex_id
        
    # Get customer reference, possible overridden on the command line        
    if customer_reference:
        fc_param["customer_reference"] = customer_reference

    # Create the tables
    snt_header = ['SciLifeLab ID','Submitted ID']
    sample_name_table = [snt_header]
    
    syt_header = ['SciLifeLab ID','Lane','Barcode']
    syt_header.append('Read{}s (M)'.format(' pair' if fc_param['is_paired'] else ''))
    sample_yield_table = [syt_header]
    
    lyt_header = ['Lane',
                  'Read{}s (M)'.format(' pair' if fc_param['is_paired'] else '')]
    
    lane_yield_table = [lyt_header] + [[lane,
                                        "{}{}".format(_round_read_count_in_millions(yld),
                                                      "*" if fc_param["application"] == "Finished library" and yld < int(fc_param["lane_yield_cutoff"]) else "")] for lane,yld in fc_param["lane_yields"].items()]
    fc_param["lane_yield_cutoff"] = _round_read_count_in_millions(fc_param["lane_yield_cutoff"])
    
    sqt_header = ['SciLifeLab ID','Lane','Barcode','Avg Q','Q30 (%)','PhiX error rate (%)']
    sample_quality_table = [sqt_header]
    
    
    # Get the list of sample runs
    sample_run_list = _set_sample_run_list(project_name, flowcell, project_alias, s_con)
    if len(sample_run_list) == 0:
        LOG.warn("No samples for project '{}', flowcell '{}'. Maybe there are no sample run metrics in statusdb?".format(project_name, flowcell))
        return output_data
    
    # Verify that the same sample does not have two entries for the same flowcell, lane and barcode. If so, prompt for input.
    sample_dict = {}
    for sample in sample_run_list:
        key = "{}_{}_{}_{}".format(sample.get("date"),
                                   sample.get("flowcell"),
                                   sample.get("lane"),
                                   sample.get("sequence"))
        if key not in sample_dict:
            sample_dict[key] = []
        
        sample_dict[key].append(sample)
        
    for key, samples in sample_dict.items():
        # If we have just one sample for this key, everything is in order
        if len(samples) == 1:
            continue
            
        # Else, we need to resolve the conflict
        LOG.warn("There are {} entries in the samples database for Date: {}, Flowcell: {}, Lane: {}, Index: {}".format(str(len(samples)),*key.split("_")))
        while len(samples) > 1:
            keep = []
            for sample in samples:
                LOG.info("Project: {}, sample: {}, yield (M): {}, _id: {}".format(sample.get("sample_prj"),
                                                                                  sample.get("barcode_name"),
                                                                                  _round_read_count_in_millions(sample.get("bc_count")),
                                                                                  sample.get("_id")))
                if not query_yes_no("Do you want to include this sample in the report?"):
                    LOG.info("Excluding sample run with _id: {}".format(sample.get("_id")))
                else:
                    keep.append(sample)
            samples = keep
        
        sample_dict[key] = samples
    
    # Populate the sample run list with the verified sample runs
    sample_run_list = [s[0] for s in sample_dict.values()]
    
    # Loop samples and build the sample information table
    for s in sample_run_list: 
        LOG.debug("working on sample '{}', sample run metrics name '{}', id '{}'".format(s.get("barcode_name", None), s.get("name", None), s.get("_id", None)))
        
        # Get the project sample name corresponding to the sample run
        project_sample = p_con.get_project_sample(project_name, s.get("project_sample_name", None))
        if project_sample:
            # FIXME: Is this really necessary? There doesn't seem to be any consequence if the ids don't match
            LOG.debug("project sample run metrics mapping found: '{}' : '{}'".format(s["name"], project_sample["sample_name"]))
            project_sample_item = project_sample['project_sample']
            # Set project_sample_d: a dictionary mapping from sample run metrics name to sample run metrics database id
            project_sample_d = _set_project_sample_dict(project_sample_item, source)
            if not project_sample_d:
                LOG.warn("No sample_run_metrics information for sample '{}', barcode name '{}', id '{}'\n\tProject summary information {}".format(s["name"], s["barcode_name"], s["_id"], project_sample))
            # Check if sample run metrics name present in project database: if so, verify that database ids are consistent
            if s["name"] not in project_sample_d.keys():
                LOG.warn("no such sample run metrics '{}' in project sample run metrics dictionary".format(s["name"]) )
            else:
                if s["_id"] == project_sample_d[s["name"]]:
                    LOG.debug("project sample run metrics mapping found: '{}' : '{}'".format(s["name"], project_sample_d[s["name"]]))
                else:
                    LOG.warn("inconsistent mapping for '{}': '{}' != '{}' (project summary id)".format(s["name"], s["_id"], project_sample_d[s["name"]]))
            s['customer_name'] = project_sample_item.get("customer_name", None)
            
        # No project sample found. Manual upload to database necessary.
        else:
            s['customer_name'] = None
            LOG.warn("No project sample name found for sample run name '{}'".format(s["barcode_name"]))
            LOG.info("Please run 'pm qc upload-qc FLOWCELL_ID --extensive-matching' to update project sample names ")
            LOG.info("or 'pm qc update --sample_prj PROJECT_NAME --names BARCODE_TO_SAMPLE_MAP to update project sample names.")
            LOG.info("Please refer to the pm documentation for examples.")
            query_ok(force=kw.get("force", False))
        
        # Get read counts, possible overridden on the command line
        if bc_count:
            read_count = _round_read_count_in_millions(_get_bc_count(s["barcode_name"], bc_count, s))
        else:
            read_count = _round_read_count_in_millions(s.get("bc_count",None))
        
        # Get quality score from demultiplex stats, if that fails
        # (which it shouldn't), fall back on fastqc data.
        (avg_quality_score, pct_q30_bases) = fc_con.get_barcode_lane_statistics(project_name, s.get("barcode_name"), fc, s["lane"])
        if not avg_quality_score:
            avg_quality_score = calc_avg_qv(s) 
        if not avg_quality_score:
            LOG.warn("Setting average quality failed for sample {}, id {}".format(s.get("name"), s.get("_id")))
        if not pct_q30_bases:
            LOG.warn("Setting % of >= Q30 Bases (PF) failed for sample {}, id {}".format(s.get("name"), s.get("_id")))
        
        # Get phix error rate, possible overridden on the command line
        if phix:
            phix_rate = _get_phix_error_rate(s["lane"], phix)
        else:
            phix_rate = fc_param["phix_error_rate"][s["lane"]] 
        
        scilifeid = s.get("project_sample_name", None)
        customerid = s.get("customer_name", None)
        lane = s.get("lane",None)
        barcode = s.get("sequence",None)
        
        sample_name_table.append([scilifeid,customerid])
        sample_yield_table.append([scilifeid,lane,barcode,read_count])
        sample_quality_table.append([scilifeid,
                                     lane,
                                     barcode,
                                     avg_quality_score,
                                     "{}{}".format(pct_q30_bases,
                                                   "*" if float(pct_q30_bases) < fc_param["sample_q30_cutoff"] else ""),
                                     "{}{}".format(phix_rate,
                                                   "*" if float(phix_rate) > fc_param["phix_cutoff"] else "")])

    # Sort the tables by smaple and lane
    snt = [sample_name_table[0]] 
    for n in sorted(sample_name_table[1:], key=operator.itemgetter(0,1)): 
        if n not in snt:
            snt.append(n)
    sample_name_table = snt
    sample_yield_table = [sample_yield_table[0]] + sorted(sample_yield_table[1:], key=operator.itemgetter(0,2,3))
    lane_yield_table = [lane_yield_table[0]] + sorted(lane_yield_table[1:], key=operator.itemgetter(0))
    sample_quality_table = [sample_quality_table[0]] + sorted(sample_quality_table[1:], key=operator.itemgetter(0,2,3))

    # Write final output to reportlab and rst files
    output_data["debug"].write(json.dumps({'s_param': [fc_param], 'sample_runs':{s["name"]:s["barcode_name"] for s in sample_run_list}}))

    make_rest_note(fc_param["rstfile"], 
                   tables={'name': sample_name_table, 'sample_yield': sample_yield_table, 'lane_yield': lane_yield_table, 'quality': sample_quality_table}, 
                   report="sample_report", **fc_param)
    
    return output_data

def _get_qc_thresholds(params, config):
    """Get the specified QC thresholds from the config
    """
    info = {}
    
    # Get the PhiX error rate cutoff
    info['phix_cutoff'] = float(config.get("qc","phix_error_rate_threshold"))
    
    # Get the expected lane yield
    [hiseq_ho, hiseq_rm, miseq] = [False,False,False]
    lane_yield = None
    instr = params.get("instrument_version")
    if instr == "MiSeq":
        miseq = True
    elif instr.startswith("HiSeq"):
        if params.get("run_mode") == "RapidRun":
            hiseq_rm = True
        else:
            hiseq_ho = True
            
    if hiseq_ho:
        lane_yield = int(config.get("qc","hiseq_ho_lane_yield"))
    elif hiseq_rm:
        lane_yield = int(config.get("qc","hiseq_rm_lane_yield"))
    elif miseq:
        lane_yield = int(config.get("qc","miseq_lane_yield"))
    info['lane_yield_cutoff'] = lane_yield

    # Get the sample quality value cutoff
    cycles = params.get("num_cycles")
    pctq30 = 0
    for level in [250,150,100,50]:
        if cycles >= level:
            if miseq:
                pctq30 = int(config.get("qc","miseq_q30_{}".format(str(level))))
            elif hiseq_ho:
                pctq30 = int(config.get("qc","hiseq_ho_q30_{}".format(str(level))))
            elif hiseq_rm:
                pctq30 = int(config.get("qc","hiseq_rm_q30_{}".format(str(level))))
            break
    info['sample_q30_cutoff'] = pctq30
    
    return info

def _get_flowcell_info(fc_con, fc, project_name=None):
    info = {}
    info["FC_id"] = fc_con.get_run_info(fc).get("Flowcell")
    info["FC_position"] = fc_con.get_run_parameters(fc).get("FCPosition")
    info["start_date"] = fc_con.get_start_date(fc)
    # Get instrument
    info['instrument_version'] = fc_con.get_instrument_type(fc)
    info['instrument_id'] = fc_con.get_instrument(fc)
    # Get run mode
    info["run_mode"] = fc_con.get_run_mode(fc)
    info["is_paired"] = fc_con.is_paired_end(fc)
    if info["is_paired"] is None:
        LOG.warn("Could not determine run setup for flowcell {}. Will assume paired-end.".format(fc))
        info["is_paired"] = True
    info["is_dual_index"] = fc_con.is_dual_index(fc)
    info["clustered"] = fc_con.get_clustered(fc)
    info["run_setup"] = fc_con.get_run_setup(fc)
    info["num_cycles"] = fc_con.num_cycles(fc)
    info["lane_yields"] = fc_con.get_lane_yields(fc,project_name)
    info["phix_error_rate"] = {lane: fc_con.get_phix_error_rate(fc,lane) for lane in info["lane_yields"].keys()}
    
    return info
    
    
def _get_project_info(project):
    info = {}
    info["project_name"] = project.get("project_name")
    info["customer_reference"] = project.get("customer_reference")
    info["application"] = project.get("application")
    info["lanes_ordered"] = project.get("details",{}).get("sequence_units_ordered_(lanes)")
    info["no_samples"] = project.get("no_of_samples")
    info["uppnex_project_id"] = project.get("uppnex_id")
    info["project_id"] = project.get("project_id")
    info["open_date"] = project.get("open_date")
    
    return info

def _exclude_sample_id(exclude_sample_ids, sample_name, barcode_seq):
    """Check whether we should exclude a sample id.

    :param exclude_sample_ids: dictionary of sample:barcode pairs
    :param sample_name: project sample name
    :param barcode_seq: the barcode sequence

    :returns: True if exclude, False otherwise
    """
    if exclude_sample_ids and sample_name in exclude_sample_ids.keys():
        if exclude_sample_ids[sample_name]:
            if barcode_seq in exclude_sample_ids[sample_name]:
                LOG.info("excluding sample '{}' with barcode '{}' from project report".format(sample_name, barcode_seq))
                return True
            else:
                LOG.info("keeping sample '{}' with barcode '{}' in sequence report".format(sample_name, barcode_seq))
                return False
        else:
            LOG.info("excluding sample '{}' from project report".format(sample_name))
            return True


def _set_sample_table_values(sample_name, project_sample, barcode_seq, ordered_million_reads, param):
    """Set the values for a sample that is to appear in the final table.

    :param sample_name: string identifier of sample
    :param project_sample: project sample dictionary from project summary database
    :param barcode_seq: barcode sequence
    :param ordered_million_reads: the number of ordered reads
    :param param: project parameters

    :returns: vals, a dictionary of table values
    """
    prjs_to_table = {'ScilifeID':'scilife_name', 'SubmittedID':'customer_name', 'MSequenced':'m_reads_sequenced'}#, 'MOrdered':'min_m_reads_per_sample_ordered', 'Status':'status'}
    vals = {x:project_sample.get(prjs_to_table[x], None) for x in prjs_to_table.keys()}
    # Set status
    vals['Status'] = project_sample.get("status", "N/A")
    if ordered_million_reads:
        param["ordered_amount"] = _get_ordered_million_reads(sample_name, ordered_million_reads)
    vals['MOrdered'] = param["ordered_amount"]
    vals['BarcodeSeq'] = barcode_seq
    vals.update({k:"N/A" for k in vals.keys() if vals[k] is None or vals[k] == ""})
    return vals

def data_delivery_note(**kw):
    """Create an easily parseable information file with information about the data delivery
    """
    output_data = {'stdout':StringIO(), 'stderr':StringIO(), 'debug':StringIO()}
    
    project_name = kw.get('project_name',None)
    flowcell = kw.get('flowcell',None)
    LOG.debug("Generating data delivery note for project {}{}.".format(project_name,' and flowcell {}'.format(flowcell if flowcell else '')))
    
    # Get a connection to the project and sample databases
    p_con = ProjectSummaryConnection(**kw)
    assert p_con, "Could not connect to project database"
    s_con = SampleRunMetricsConnection(**kw)
    assert s_con, "Could not connect to sample database"
    
    # Get the entry for the project and samples from the database
    LOG.debug("Fetching samples from sample database")
    samples = s_con.get_samples(sample_prj=project_name, fc_id=flowcell)
    LOG.debug("Got {} samples from database".format(len(samples)))
    
    # Get the customer sample names from the project database
    LOG.debug("Fetching samples from project database")
    project_samples = p_con.get_entry(project_name, "samples")
    customer_names = {sample_name:sample.get('customer_name','N/A') for sample_name, sample in project_samples.items()}
    
    data = [['SciLifeLab ID','Submitted ID','Flowcell','Lane','Barcode','Read','Path','MD5','Size (bytes)','Timestamp']]
    for sample in samples:
        sname = sample.get('project_sample_name','N/A')
        cname = customer_names.get(sname,'N/A')
        fc = sample.get('flowcell','N/A')
        lane = sample.get('lane','N/A')
        barcode = sample.get('sequence','N/A')
        if 'raw_data_delivery' not in sample:
            data.append([sname,cname,'','','','','','','',''])
            continue
        delivery = sample['raw_data_delivery']
        tstamp = delivery.get('timestamp','N/A')
        for read, file in delivery.get('files',{}).items():
            data.append([sname,
                         cname,
                         fc,
                         lane,
                         barcode,
                         read,
                         file.get('path','N/A'),
                         file.get('md5','N/A'),
                         file.get('size_in_bytes','N/A'),
                         tstamp,])
    
    # Write the data to a csv file
    outfile = "{}{}_data_delivery.csv".format(project_name,'_{}'.format(flowcell) if flowcell else '')
    LOG.debug("Writing delivery data to {}".format(outfile))
    with open(outfile,"w") as outh:
        csvw = csv.writer(outh)
        for row in data:
            csvw.writerow(row)
    
    # Write Texttable formatted output to stdout
    tt = texttable.Texttable(180)
    tt.add_rows(data)
    output_data['stdout'].write(tt.draw())
        
    return output_data
    

def project_status_note(project_name=None, username=None, password=None, url=None,
                        use_ps_map=True, use_bc_map=False, check_consistency=False,
                        ordered_million_reads=None, uppnex_id=None, customer_reference=None,
                        exclude_sample_ids={}, project_alias=None, sample_aliases={},
                        projectdb="projects", samplesdb="samples", flowcelldb="flowcells",
                        include_all_samples=False, flat_table=False, **kw):
    """Make a project status note. Used keywords:

    :param project_name: project name
    :param user: db user name
    :param password: db password
    :param url: db url
    :param use_ps_map: use project summary mapping
    :param use_bc_map: use project to barcode name mapping
    :param check_consistency: check consistency between mappings
    :param ordered_million_reads: number of ordered reads in millions
    :param uppnex_id: the uppnex id
    :param customer_reference: customer project name
    :param exclude_sample_ids: exclude some sample ids from project note
    :param project_alias: project alias name
    :param sample_aliases: sample alias names
    :param projectdb: project db name
    :param samplesdb: samples db name
    :param flowcelldb: flowcells db name
    :param include_all_samples: include all samples in report
    :param flat_table: Just create a simple tab-separated version of the table instead of the fancy pdf
    """

    # parameters
    parameters = {
        "project_name" : project_name,
        "finished" : "Not finished, or cannot yet assess if finished.",
        }

    output_data, sample_table, param = _project_status_note_table(project_name, username, password, url,
                                                                  use_ps_map, use_bc_map, check_consistency,
                                                                  ordered_million_reads, uppnex_id,
                                                                  customer_reference, exclude_sample_ids,
                                                                  project_alias, sample_aliases, projectdb,
                                                                  samplesdb, flowcelldb, include_all_samples,
                                                                  parameters, **kw)

    if not flat_table:
        # Set report paragraphs
        paragraphs = project_note_paragraphs()
        headers = project_note_headers()

        paragraphs["Samples"]["tpl"] = make_sample_table(sample_table)
        make_note("{}_project_summary.pdf".format(project_name), headers, paragraphs, **param)
        make_rest_note("{}_project_summary.rst".format(project_name), sample_table=sample_table, report="project_report", **param)

    else:
        # Write tab-separated output
        sample_table[0].insert(0,'ProjectID')
        table_cols = [sample_table[0].index(col) for col in ['ProjectID', 'ScilifeID', 'SubmittedID', 'BarcodeSeq', 'MSequenced']]
        outfile = "{}_project_summary.csv".format(project_name)
        with open(outfile,"w") as outh:
            csvw = csv.writer(outh)
            for i,sample in enumerate(sample_table):
                if i > 0:
                    sample.insert(0,project_name)
                data = [str(sample[col]) for col in table_cols]
                csvw.writerow(data)
                output_data['stdout'].write("{}\n".format("\t".join(data)))

    param.update({k:"N/A" for k in param.keys() if param[k] is None or param[k] ==  ""})
    output_data["debug"].write(json.dumps({'param':param, 'table':sample_table}))

    return output_data


def _project_status_note_table(project_name=None, username=None, password=None, url=None,
                               use_ps_map=True, use_bc_map=False, check_consistency=False,
                               ordered_million_reads=None, uppnex_id=None, customer_reference=None,
                               exclude_sample_ids={}, project_alias=None, sample_aliases={},
                               projectdb="projects", samplesdb="samples", flowcelldb="flowcells",
                               include_all_samples=False, param={}, **kw):

    # mapping project_summary to parameter keys
    ps_to_parameter = {"scilife_name":"scilife_name", "customer_name":"customer_name", "project_name":"project_name"}
    # mapping project sample to table
    table_keys = ['ScilifeID', 'SubmittedID', 'BarcodeSeq', 'MSequenced', 'MOrdered', 'Status']

    output_data = {'stdout':StringIO(), 'stderr':StringIO(), 'debug':StringIO()}
    # Connect and run
    s_con = SampleRunMetricsConnection(dbname=samplesdb, username=username, password=password, url=url)
    fc_con = FlowcellRunMetricsConnection(dbname=flowcelldb, username=username, password=password, url=url)
    p_con = ProjectSummaryConnection(dbname=projectdb, username=username, password=password, url=url)

    #Get the information source for this project
    source = p_con.get_info_source(project_name)

    # Get project summary from project database
    sample_aliases = _literal_eval_option(sample_aliases, default={})
    prj_summary = p_con.get_entry(project_name)
    if not prj_summary:
        LOG.warn("No such project '{}'".format(project_name))
        return
    LOG.debug("Working on project '{}'.".format(project_name))

    # Get sample run list and loop samples to make mapping sample -> {sampleruns}
    sample_run_list = _set_sample_run_list(project_name, flowcell=None, project_alias=project_alias, s_con=s_con)
    samples = {}
    for s in sample_run_list:
        prj_sample = p_con.get_project_sample(project_name, s.get("project_sample_name", None))
        if prj_sample:
            sample_name = prj_sample['project_sample'].get("scilife_name", None)
            s_d = {s["name"] : {'sample':sample_name, 'id':s["_id"]}}
            samples.update(s_d)
        else:
            if s["barcode_name"] in sample_aliases:
                s_d = {sample_aliases[s["barcode_name"]] : {'sample':sample_aliases[s["barcode_name"]], 'id':s["_id"]}}
                samples.update(s_d)
            else:
                s_d = {s["name"]:{'sample':s["name"], 'id':s["_id"], 'barcode_name':s["barcode_name"]}}
                LOG.warn("No mapping found for sample run:\n  '{}'".format(s_d))

    # Convert to mapping from desired sample name to list of aliases
    # Less important for the moment; one solution is to update the
    # Google docs summary table to use the P names
    sample_dict = prj_summary['samples']
    param.update({key:prj_summary.get(ps_to_parameter[key], None) for key in ps_to_parameter.keys()})
    param["ordered_amount"] = param.get("ordered_amount", p_con.get_ordered_amount(project_name, samples=sample_dict))
    param['customer_reference'] = param.get('customer_reference', prj_summary.get('customer_reference'))
    param['uppnex_project_id'] = param.get('uppnex_project_id', prj_summary.get('uppnex_id'))

    # Override database values if options passed at command line
    if uppnex_id:
        param["uppnex_project_id"] = uppnex_id
    if customer_reference:
        param["customer_reference"] = customer_reference

    # Process options
    ordered_million_reads = _literal_eval_option(ordered_million_reads)
    exclude_sample_ids = _literal_eval_option(exclude_sample_ids, default={})

    ## Start collecting the data
    sample_table = []
    samples_excluded = []
    all_passed = True
    last_library_preps = p_con.get_latest_library_prep(project_name)
    last_library_preps_srm = [x for l in last_library_preps.values() for x in l]
    LOG.debug("Looping through sample map that maps project sample names to sample run metrics ids")
    for k,v in samples.items():
        LOG.debug("project sample '{}' maps to '{}'".format(k, v))
        if not include_all_samples:
            if v['sample'] not in last_library_preps.keys():
                LOG.info("No library prep information for sample {}; keeping in report".format(v['sample']))
            else:
                if k not in last_library_preps_srm:
                    LOG.info("Sample run {} ('{}') is not latest library prep ({}) for project sample {}: excluding from report".format(k, v["id"], ",".join(list(set(last_library_preps[v['sample']].values()))), v['sample']))
                    continue
        else:
            pass

        if re.search("Unexpected", k):
            continue
        barcode_seq = s_con.get_entry(k, "sequence")
        # Exclude sample id?
        if _exclude_sample_id(exclude_sample_ids, v['sample'], barcode_seq):
            samples_excluded.append(v['sample'])
            continue
        # Get the project sample name from the sample run and set table values
        project_sample = sample_dict[v['sample']]
        vals = _set_sample_table_values(v['sample'], project_sample, barcode_seq, ordered_million_reads, param)
        if vals['Status']=="N/A" or vals['Status']=="NP": all_passed = False
        sample_table.append([vals[k] for k in table_keys])

    # Loop through samples in sample_dict for which there is no sample run information
    samples_in_table_or_excluded = list(set([x[0] for x in sample_table])) + samples_excluded
    samples_not_in_table = list(set(sample_dict.keys()) - set(samples_in_table_or_excluded))
    for sample in samples_not_in_table:
        if re.search("Unexpected", sample):
            continue
        project_sample = sample_dict[sample]
        # Set project_sample_d: a dictionary mapping from sample run metrics name to sample run metrics database id
        project_sample_d = _set_project_sample_dict(project_sample, source)
        if project_sample_d:
            for k,v in project_sample_d.iteritems():
                barcode_seq = s_con.get_entry(k, "sequence")
                vals = _set_sample_table_values(sample, project_sample, barcode_seq, ordered_million_reads, param)
                if vals['Status']=="N/A" or vals['Status']=="NP": all_passed = False
                sample_table.append([vals[k] for k in table_keys])
        else:
            barcode_seq = None
            vals = _set_sample_table_values(sample, project_sample, barcode_seq, ordered_million_reads, param)
            if vals['Status']=="N/A" or vals['Status']=="NP": all_passed = False
            sample_table.append([vals[k] for k in table_keys])
    if all_passed: param["finished"] = 'Project finished.'
    sample_table.sort()
    sample_table = list(sample_table for sample_table,_ in itertools.groupby(sample_table))
    sample_table.insert(0, ['ScilifeID', 'SubmittedID', 'BarcodeSeq', 'MSequenced', 'MOrdered', 'Status'])

    return output_data, sample_table, param

