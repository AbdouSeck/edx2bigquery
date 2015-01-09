#!/usr/bin/python

import os
import csv
import gsutil
import bqutil
import json
import datetime

from collections import defaultdict, OrderedDict
from lxml import etree
from path import path
from load_course_sql import find_course_sql_dir

CCDATA = "course_content.csv"
CMINFO = "course_metainfo.csv"
CMINFO_OVERRIDES = "course_metainfo_overrides.csv"

def get_stats_module_usage(course_id,
                           basedir="X-Year-2-data-sql", 
                           datedir="2013-09-21", 
                           use_dataset_latest=False,
                           ):
    '''
    Get data from the stats_module_usage table, if it doesn't already exist as a local file.
    Compute it if necessary.
    '''
    dataset = bqutil.course_id2dataset(course_id, use_dataset_latest=use_dataset_latest)

    sql = """   SELECT 
                    module_type, module_id, count(*) as ncount 
                FROM [{dataset}.studentmodule] 
                group by module_id, module_type
                order by module_id
          """.format(dataset=dataset)

    table = 'stats_module_usage'
    course_dir = find_course_sql_dir(course_id, basedir, datedir, use_dataset_latest)
    csvfn = course_dir / (table + ".csv")

    data = {}
    if csvfn.exists():
        # read file into data structure
        for k in list(csv.DictReader(open(csvfn))):
            midfrag = tuple(k['module_id'].split('/')[-2:])
            data[midfrag] = k
    else:
        # download if it is already computed, or recompute if needed
        bqdat = bqutil.get_bq_table(dataset, table, sql=sql)
        if bqdat is None:
            bqdat = {'data': []}

        fields = [ "module_type", "module_id", "ncount" ]
        fp = open(csvfn, 'w')
        cdw = csv.DictWriter(fp, fieldnames=fields)
        cdw.writeheader()
        for k in bqdat['data']:
            midfrag = tuple(k['module_id'].split('/')[-2:])
            data[midfrag] = k
            try:
                k['module_id'] = k['module_id'].encode('utf8')
                cdw.writerow(k)
            except Exception as err:
                print "Error writing row %s, err=%s" % (k, str(err))
        fp.close()

    print "[analyze_content] got %d lines of studentmodule usage data" % len(data)
    return data


def analyze_course_content(course_id, 
                           listings_file=None,
                           basedir="X-Year-2-data-sql", 
                           datedir="2013-09-21", 
                           use_dataset_latest=False,
                           do_upload=False,
                           courses=None,
                           verbose=True,
                           ):
    '''
    Compute course_content table, which quantifies:

    - number of chapter, sequential, vertical modules
    - number of video modules
    - number of problem, *openended, mentoring modules
    - number of dicussion, annotatable, word_cloud modules

    Do this using the course "xbundle" file, produced when the course axis is computed.

    Include only modules which had nontrivial use, to rule out the staff and un-shown content. 
    Do the exclusion based on count of module appearing in the studentmodule table, based on 
    stats_module_usage for each course.

    Also, from the course listings file, compute the number of weeks the course was open.

    if do_upload then upload all accumulated data to the course report dataset as the "stats_course_content" table.
    '''

    if do_upload:
        if use_dataset_latest:
            crname = 'course_report_latest'
        else:
            org = courses[0].split('/',1)[0]	# extract org from first course_id in courses
            crname = 'course_report_%s' % org

        gspath = gsutil.gs_path_from_course_id(crname)
        gsfnp = gspath / CCDATA
        gsutil.upload_file_to_gs(CCDATA, gsfnp)
        tableid = "stats_course_content"
        dataset = crname

        mypath = os.path.dirname(os.path.realpath(__file__))
        SCHEMA_FILE = '%s/schemas/schema_content_stats.json' % mypath

        try:
            the_schema = json.loads(open(SCHEMA_FILE).read())[tableid]
        except Exception as err:
            print "Oops!  Failed to load schema file for %s.  Error: %s" % (tableid, str(err))
            raise

        bqutil.load_data_to_table(dataset, tableid, gsfnp, the_schema, wait=True, verbose=False,
                                  format='csv', skiprows=1)

        table = 'course_metainfo'
        sql = "select * from {courses}".format(courses='\n'.join([('[%s.course_metainfo]' % x) for x in courses]))
        print "--> Creating %s.%s using %s" % (dataset, table, sql)

        bqutil.create_bq_table(dataset, table, sql)

        return

    
    print "-"*60 + " %s" % course_id

    # get nweeks from listings
    lfn = path(listings_file)
    if not lfn.exists():
        print "[analyze_content] course listings file %s doesn't exist!" % lfn
        return

    data = None
    for k in csv.DictReader(open(lfn)):
        if k['course_id']==course_id:
            data = k
            break

    if not data:
        print "[analyze_content] no entry for %s found in course listings file %s!" % (course_id, lfn)
        return

    def date_parse(field):
        (m, d, y) = map(int, data[field].split('/'))
        return datetime.datetime(y, m, d)

    launch = date_parse('Course Launch')
    wrap = date_parse('Course Wrap')
    ndays = (wrap - launch).days
    nweeks = ndays / 7.0

    print "Course length = %6.2f weeks (%d days)" % (nweeks, ndays)

    course_dir = find_course_sql_dir(course_id, basedir, datedir, use_dataset_latest)
    cfn = gsutil.path_from_course_id(course_id)

    xbfn = course_dir / ("xbundle_%s.xml" % cfn)
    
    if not xbfn.exists():
        print "[analyze_content] cannot find xbundle file %s for %s!" % (xbfn, course_id)
        return

    print "[analyze_content] For %s using %s" % (course_id, xbfn)
    
    # get module usage data
    mudata = get_stats_module_usage(course_id, basedir, datedir, use_dataset_latest)

    xml = etree.parse(open(xbfn)).getroot()
    
    counts = defaultdict(int)
    nexcluded = defaultdict(int)

    IGNORE = ['html', 'p', 'div', 'iframe', 'ol', 'li', 'ul', 'blockquote', 'h1', 'em', 'b', 'h2', 'h3', 'body', 'span', 'strong',
              'a', 'sub', 'strike', 'table', 'td', 'tr', 's', 'tbody', 'sup', 'sub', 'strike', 'i', 's', 'pre', 'policy', 'metadata',
              'grading_policy', 'br', 'center',  'wiki', 'course', 'font', 'tt', 'it', 'dl', 'startouttext', 'endouttext', 'h4', 
              'head', 'source', 'dt', 'hr', 'u', 'style', 'dd', 'script', 'th', 'p', 'P', 'TABLE', 'TD', 'small', 'text', 'title']

    def walk_tree(elem):
        if  type(elem.tag)==str and (elem.tag.lower() not in IGNORE):
            counts[elem.tag.lower()] += 1
        for k in elem:
            midfrag = (k.tag, k.get('url_name_orig', None))
            if (midfrag in mudata) and int(mudata[midfrag]['ncount']) < 20:
                nexcluded[k.tag] += 1
                if verbose:
                    print "    -> excluding %s (%s), ncount=%s" % (k.get('display_name', '<no_display_name>').encode('utf8'), 
                                                                   midfrag, 
                                                                   mudata.get(midfrag, {}).get('ncount'))
                continue
            walk_tree(k)

    walk_tree(xml)
    print counts

    # combine some into "qual_axis" and others into "quant_axis"
    qual_axis = ['openassessment', 'optionresponse', 'multiplechoiceresponse', 
                 # 'discussion', 
                 'choiceresponse', 'word_cloud', 
                 'combinedopenended', 'choiceresponse', 'stringresponse', 'textannotation', 'openended', 'lti']
    quant_axis = ['formularesponse', 'numericalresponse', 'customresponse', 'symbolicresponse', 'coderesponse',
                  'imageresponse']

    nqual = 0
    nquant = 0
    for tag, count in counts.items():
        if tag in qual_axis:
            nqual += count
        if tag in quant_axis:
            nquant += count
    
    print "nqual=%d, nquant=%d" % (nqual, nquant)

    nqual_per_week = nqual / nweeks
    nquant_per_week = nquant / nweeks
    total_per_week = nqual_per_week + nquant_per_week

    print "per week: nqual=%6.2f, nquant=%6.2f total=%6.2f" % (nqual_per_week, nquant_per_week, total_per_week)

    # save this overall data in CCDATA
    ccdfn = path(CCDATA)
    ccd = {}
    if ccdfn.exists():
        for k in csv.DictReader(open(ccdfn)):
            ccd[k['course_id']] = k
    
    ccd[course_id] = {'course_id': course_id,
                      'nweeks': nweeks,
                      'nqual_per_week': nqual_per_week,
                      'nquant_per_week': nquant_per_week,
                      'total_assessments_per_week' : total_per_week,
                      }

    # fields = ccd[ccd.keys()[0]].keys()
    fields = ['course_id', 'nquant_per_week', 'total_assessments_per_week', 'nqual_per_week', 'nweeks']
    cfp = open(ccdfn, 'w')
    dw = csv.DictWriter(cfp, fieldnames=fields)
    dw.writeheader()
    for cid, entry in ccd.items():
        dw.writerow(entry)
    cfp.close()

    # store data in course_metainfo table, which has one (course_id, key, value) on each line
    # keys include nweeks, nqual, nquant, count_* for module types *

    cmfields = OrderedDict()
    cmfields['course_id'] = course_id
    cmfields['course_length_days'] = str(ndays)
    cmfields.update({ ('listings_%s' % key) : value for key, value in data.items() })	# from course listings
    cmfields.update(ccd[course_id].copy())
    cmfields.update({ ('count_%s' % key) : str(value) for key, value in counts.items() })	# from content counts
    cmfields.update({ ('nexcluded_sub_20_%s' % key) : str(value) for key, value in nexcluded.items() })	# from content counts

    course_dir = find_course_sql_dir(course_id, basedir, datedir, use_dataset_latest)
    csvfn = course_dir / CMINFO

    # manual overriding of the automatically computed fields can be done by storing course_id,key,value data
    # in the CMINFO_OVERRIDES file

    csvfn_overrides = course_dir / CMINFO_OVERRIDES
    if csvfn_overrides.exists():
        print "--> Loading manual override information from %s" % csvfn_overrides
        for ovent in csv.DictReader(open(csvfn_overrides)):
            if not ovent['course_id']==course_id:
                print "===> ERROR! override file has entry with wrong course_id: %s" % ovent
                continue
            print "    overriding key=%s with value=%s" % (ovent['key'], ovent['value'])
            cmfields[ovent['key']] = ovent['value']

    print "--> Course metainfo writing to %s" % csvfn

    fp = open(csvfn, 'w')

    cdw = csv.DictWriter(fp, fieldnames=['course_id', 'key', 'value'])
    cdw.writeheader()
    
    for k, v in cmfields.items():
        cdw.writerow({'course_id': course_id, 'key': k, 'value': v})
        
    fp.close()

    table = 'course_metainfo'
    dataset = bqutil.course_id2dataset(course_id, use_dataset_latest=use_dataset_latest)

    gsfnp = gsutil.gs_path_from_course_id(course_id, use_dataset_latest=use_dataset_latest) / CMINFO
    print "--> Course metainfo uploading to %s then to %s.%s" % (gsfnp, dataset, table)

    gsutil.upload_file_to_gs(csvfn, gsfnp)

    mypath = os.path.dirname(os.path.realpath(__file__))
    SCHEMA_FILE = '%s/schemas/schema_course_metainfo.json' % mypath
    the_schema = json.loads(open(SCHEMA_FILE).read())[table]

    bqutil.load_data_to_table(dataset, table, gsfnp, the_schema, wait=True, verbose=False, format='csv', skiprows=1)
