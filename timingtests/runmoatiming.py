#!/usr/bin/env python3
"""
Run a series of moamosaic jobs, doing 3x3 tile mosaics of Sentinel-2
inputs, for varying numbers of read threads.

Input is a STAC search results file from searchStac.py.
Output is a JSON file of monitoring information, containing timings and
other info.

"""
import argparse
import json

from osgeo import gdal

from moa import moamosaic


bandList = ['B02', 'B03', 'B04', 'B08']


def getCmdargs():
    """
    Get command line arguments
    """
    p = argparse.ArgumentParser()
    p.add_argument("--stacresults", help=("JSON file of pre-computed " +
        "STAC search results"))
    p.add_argument("--monitorjson", default="fullrun.stats.json",
        help="Name of JSON file to save monitoring info (default=%(default)s)")
    p.add_argument("--maxnumthreads", default=5, type=int,
        help=("Maximum number of threads to use in mosaic runs " +
                "(default=%(default)s)"))
    p.add_argument("--blocksize", default=1024, type=int,
        help="Blocksize (in pixels) (default=%(default)s)")

    cmdargs = p.parse_args()

    return cmdargs


def main():
    cmdargs = getCmdargs()
    gdal.UseExceptions()

    tilesByDate = json.load(open(cmdargs.stacresults))

    mosaicJobList = genJoblist(tilesByDate)
    print("Made {} mosaic jobs".format(len(mosaicJobList)))

    # For each value of numthreads, do this many mosaic jobs, to make a
    # population of runtimes.
    runsPerThreadcount = len(mosaicJobList) // cmdargs.maxnumthreads

    driver = "GTiff"
    outfile = "crap.tif"
    nopyramids = True
    monitorjson = None
    nullval = 0
    outf = open(cmdargs.monitorjson, 'w')

    monitorList = []
    i = 0
    for infileList in mosaicJobList:
        try:
            numthreads = i // runsPerThreadcount + 1
            monitorDict = moamosaic.doMosaic(infileList, outfile,
                numthreads, cmdargs.blocksize, driver, nullval,
                nopyramids, monitorjson)
            monitorList.append(monitorDict)
            print("Done job", i)
        except Exception as e:
            print("Exception {} for job {}".format(e, i))

        i += 1

    json.dump(monitorList, outf, indent=2)


def genFilelist(tileList, band):
    filelist = []
    for (tilename, path, nullPcnt) in tileList:
        vsiPath = path.replace("s3:/", "/vsis3")
        fn = "{}/{}.tif".format(vsiPath, band)
        filelist.append(fn)
    return filelist


def genJoblist(tilesByDate):
    """
    Generate a list of mosaic jobs to do. Each job is a list of
    nine adjacent tiles for a given date and band. Return a list
    of these lists.
    """
    datelist = sorted(tilesByDate.keys())
    mosaicJobList = []
    for date in datelist:
        if len(tilesByDate[date]) == 9:
            for band in bandList:
                filelist = genFilelist(tilesByDate[date], band)
                mosaicJobList.append(filelist)
    return mosaicJobList


if __name__ == "__main__":
    main()
