#!/usr/bin/env python3
"""
MoaMosaic is a tool for mosaicing larger numbers of input raster
image files into a single output raster. It uses threading to overlap
the reading of inputs from slower storage, such as S3 buckets. In many
other situations there is little advantage in this, but the latencies
involved in reading from remote files mean there is significant benefit
to reading blocks of data in parallel.

The software is named for the Moa, a group of large flightless birds
native to New Zealand (now extinct).
See https://en.wikipedia.org/wiki/Moa

MoaMosaic relies on GDAL to read and write raster files, so any format
supported by GDAL may be used. This includes all of its "/vsi" virtual
file systems, so support for files on S3 is available via /vsis3/.

"""
import os
import argparse
from concurrent import futures
import queue
import json
import shutil
from multiprocessing import cpu_count

import numpy
from osgeo import gdal
from osgeo.gdal_array import GDALTypeCodeToNumericTypeCode

from . import monitoring
from . import structures
from . import reproj


# Some default values
DFLT_NUMTHREADS = 4
DFLT_BLOCKSIZE = 1024
DFLT_DRIVER = "GTiff"
defaultCreationOptions = {
    'GTiff': ['COMPRESS=DEFLATE', 'TILED=YES', 'BIGTIFF=IF_SAFER',
        'INTERLEAVE=BAND'],
    'KEA': [],
    'HFA': ['COMPRESS=YES', 'IGNORE_UTM=TRUE']
}


def getCmdargs():
    """
    Get command line arguments
    """
    knownDrivers = ','.join(defaultCreationOptions.keys())

    p = argparse.ArgumentParser()
    p.add_argument("-i", "--infilelist", help="Text file list of input images")
    p.add_argument("-n", "--numthreads", type=int, default=4,
        help="Number of read threads to use (default=%(default)s)")
    p.add_argument("-b", "--blocksize", type=int, default=1024,
        help="Blocksize in pixels (default=%(default)s)")
    p.add_argument("-d", "--driver", default="GTiff",
        help="GDAL driver to use for output file (default=%(default)s)")
    p.add_argument("-o", "--outfile", help="Name of output raster")
    p.add_argument("--co", action="append",
        help=("Specify a GDAL creation option (as 'NAME=VALUE'). Can be " +
              "given multiple times. There are sensible default creation " +
              "options for some drivers ({}), but if this option is used, " +
              "those are ignored.").format(knownDrivers))
    p.add_argument("--nullval", type=int,
        help="Null value to use (default comes from input files)")
    p.add_argument("--omitpyramids", default=False, action="store_true",
        help="Omit the pyramid layers (i.e. overviews)")
    p.add_argument("--monitorjson", help="Output JSON file of monitoring info")
    outprojGroup = p.add_argument_group("Output Projection")
    outprojGroup.add_argument("--outprojepsg", type=int,
        help="EPSG number of desired output projection")
    outprojGroup.add_argument("--outprojwktfile",
        help="Name of text file containing WKT of desired output projection")
    outprojGroup.add_argument("--xres", type=float,
        help="Desired output X pixel size (default matches input)")
    outprojGroup.add_argument("--yres", type=float,
        help="Desired output Y pixel size (default matches input)")
    outprojGroup.add_argument("--resample", default="nearest",
        help=("GDAL name of resampling method to use for " +
            "reprojection (default=%(default)s)"))
    cmdargs = p.parse_args()
    return cmdargs


def mainCmd():
    """
    Main command line stub, referenced from pyproject.toml
    """
    gdal.UseExceptions()

    cmdargs = getCmdargs()
    filelist = makeFilelist(cmdargs.infilelist)
    monitorDict = doMosaic(filelist, cmdargs.outfile,
        numthreads=cmdargs.numthreads, blocksize=cmdargs.blocksize,
        driver=cmdargs.driver, nullval=cmdargs.nullval,
        dopyramids=(not cmdargs.omitpyramids), creationoptions=cmdargs.co,
        outprojepsg=cmdargs.outprojepsg, outprojwktfile=cmdargs.outprojwktfile,
        outXres=cmdargs.xres, outYres=cmdargs.yres,
        resamplemethod=cmdargs.resample)

    if cmdargs.monitorjson is not None:
        with open(cmdargs.monitorjson, 'w') as f:
            json.dump(monitorDict, f, indent=2)


def doMosaic(filelist, outfile, *, numthreads=DFLT_NUMTHREADS,
        blocksize=DFLT_BLOCKSIZE, driver=DFLT_DRIVER, nullval=None,
        dopyramids=True, creationoptions=None, outprojepsg=None,
        outprojwktfile=None, outprojwkt=None, outXres=None,
        outYres=None, resamplemethod=None):
    """
    Main routine, callable from non-commandline context
    """
    monitors = monitoring.Monitoring()
    monitors.setParam('numthreads', numthreads)
    monitors.setParam('blocksize', blocksize)
    monitors.setParam('cpucount', cpu_count())

    # Work out what we are going to do
    monitors.setParam('numinfiles', len(filelist))
    monitors.timestamps.stamp("imginfodict", monitoring.TS_START)
    imgInfoDict = makeImgInfoDict(filelist, numthreads)
    monitors.timestamps.stamp("imginfodict", monitoring.TS_END)

    monitors.timestamps.stamp("projection", monitoring.TS_START)
    (filelist, tmpdir) = reproj.handleProjections(filelist,
        imgInfoDict, outprojepsg, outprojwktfile, outprojwkt, outXres,
        outYres, resamplemethod, nullval)
    monitors.timestamps.stamp("projection", monitoring.TS_END)

    if nullval is None:
        nullval = imgInfoDict[filelist[0]].nullVal

    monitors.timestamps.stamp("analysis", monitoring.TS_START)
    outImgInfo = makeOutputGrid(filelist, imgInfoDict, nullval)
    blockList = makeOutputBlockList(outImgInfo, blocksize)

    (blockListWithInputs, filesForBlock) = (
        findInputsPerBlock(blockList, outImgInfo.transform, filelist,
        imgInfoDict))
    blockReadingList = makeBlockReadingList(blockListWithInputs)
    blocksPerThread = divideBlocksByThread(blockReadingList, numthreads)
    monitors.timestamps.stamp("analysis", monitoring.TS_END)

    blockQ = queue.Queue()
    poolClass = futures.ThreadPoolExecutor
    numBands = imgInfoDict[filelist[0]].numBands

    # Now do it all, using concurrent threads to read blocks into a queue
    outDs = openOutfile(outfile, driver, outImgInfo, creationoptions)
    monitors.timestamps.stamp("domosaic", monitoring.TS_START)
    for bandNum in range(1, numBands + 1):
        with poolClass(max_workers=numthreads) as threadPool:
            workerList = []
            for i in range(numthreads):
                blocksToRead = blocksPerThread[i]
                worker = threadPool.submit(readFunc, blocksToRead, blockQ,
                        bandNum, outImgInfo.nullVal)
                workerList.append(worker)

            writeFunc(blockQ, outDs, outImgInfo, bandNum,
                    blockList, filesForBlock, workerList, monitors)
    monitors.timestamps.stamp("domosaic", monitoring.TS_END)

    outDs.SetGeoTransform(outImgInfo.transform)
    outDs.SetProjection(outImgInfo.projection)
    if dopyramids:
        monitors.timestamps.stamp("pyramids", monitoring.TS_START)
        outDs.BuildOverviews(overviewlist=[4, 8, 16, 32, 64, 128, 256, 512])
        monitors.timestamps.stamp("pyramids", monitoring.TS_END)

    if tmpdir is not None:
        shutil.rmtree(tmpdir)

    return monitors.reportAsDict()


def readFunc(blocksToRead, blockQ, bandNum, outNullVal):
    """
    This function is run by all the read workers, each with its own list
    of blocks to read.
    """
    blocksPerInfile = structures.BlocksByInfile()
    for blockInfo in blocksToRead:
        blocksPerInfile.blockToDo(blockInfo.filename, blockInfo.outblock)
    gdalObjCache = structures.GdalObjCache()

    i = 0
    for blockInfo in blocksToRead:
        filename = blockInfo.filename
        (ds, band) = gdalObjCache.openBand(filename, bandNum)
        inblock = blockInfo.inblock
        (left, top, xsize, ysize) = (inblock.left, inblock.top,
                inblock.xsize, inblock.ysize)
        # Don't try to read outside the extent of the infile
        left1 = max(left, 0)
        top1 = max(top, 0)
        right1 = min(left + xsize, ds.RasterXSize)
        xsize1 = right1 - left1
        bottom1 = min(top + ysize, ds.RasterYSize)
        ysize1 = bottom1 - top1
        arr = band.ReadAsArray(left1, top1, xsize1, ysize1)

        # Now slot this possibly smaller array back into a full array,
        # with null padding.
        outArr = numpy.zeros((ysize, xsize), dtype=arr.dtype)
        outArr.fill(outNullVal)
        coloffset = max(0, -left)
        rowoffset = max(0, -top)
        outArr[rowoffset:rowoffset+ysize1, coloffset:coloffset+xsize1] = arr

        # Put the full bloc into the blockQ, along with the associated
        # block information
        blockQ.put((blockInfo, outArr))

        # If this input file is now done, we can close it.
        blocksPerInfile.blockDone(filename, blockInfo.outblock)
        if blocksPerInfile.countRemaining(filename) == 0:
            gdalObjCache.closeBand(filename, bandNum)
        i += 1


def writeFunc(blockQ, outDs, outImgInfo, bandNum,
                    blockList, filesForBlock, workerList, monitors):
    """
    Loop over all blocks of the output grid, and write them.

    Input blocks are retrieved from the blockQ, and placed in a block
    cache. When all inputs for a given output block are available,
    that block is assembled for output, merging the inputs appropriately.
    The resulting block is written to the output file.

    The input blocks are deleted from the cache. All worker processes
    are then checked for exceptions. Then move to the next output block.

    This function runs continuously for a single band of the output file,
    after which it returns. It will then be called again for the next band.

    """
    band = outDs.GetRasterBand(bandNum)

    # Cache of blocks available to write
    blockCache = structures.BlockCache()

    numOutBlocks = len(blockList)
    i = 0
    while i < numOutBlocks:
        # Get another block from the blockQ (if available), and cache it
        if not blockQ.empty():
            (blockInfo, arr) = blockQ.get_nowait()
            filename = blockInfo.filename
            outblock = blockInfo.outblock
            blockCache.add(filename, outblock, arr)
        else:
            blockInfo = None
            arr = None

        outblock = blockList[i]

        if outblock not in filesForBlock:
            # This block does not intersect any input files, so
            # just write nulls
            numpyDtype = GDALTypeCodeToNumericTypeCode(outImgInfo.dataType)
            outArr = numpy.zeros((outblock.ysize, outblock.xsize),
                    dtype=numpyDtype)
            outArr.fill(outImgInfo.nullVal)
            band.WriteArray(outArr, outblock.left, outblock.top)
            i += 1
        elif blockInfo is not None or len(blockCache) > 0:
            # If we actually got something from the blockQ, then we might
            # be ready to write the current block

            allInputBlocks = getInputsForBlock(blockCache, outblock,
                    filesForBlock)
            if allInputBlocks is not None:
                outArr = mergeInputs(allInputBlocks, outImgInfo.nullVal)
                band.WriteArray(outArr, outblock.left, outblock.top)

                # Remove all inputs from cache
                for filename in filesForBlock[outblock]:
                    blockCache.remove(filename, outblock)

                # Proceed to the next output block
                i += 1

        checkReaderExceptions(workerList)

        monitors.minMaxBlockCacheSize.update(len(blockCache))
        monitors.minMaxBlockQueueSize.update(blockQ.qsize())

    band.SetNoDataValue(outImgInfo.nullVal)


def checkReaderExceptions(workerList):
    """
    Check the read workers, in case one has raised an exception. The
    elements of workerList are futures.Future objects.
    """
    for worker in workerList:
        if worker.done():
            e = worker.exception(timeout=0)
            if e is not None:
                raise e


def allWorkersDone(workerList):
    """
    Return True if all owrkers are done
    """
    allDone = True
    for worker in workerList:
        if not worker.done():
            allDone = False
    return allDone


def makeFilelist(infilelist):
    """
    Read the list of input files, and return a list of the filenames
    """
    filelist = [line.strip() for line in open(infilelist)]
    return filelist


def makeOutputGrid(filelist, imgInfoDict, nullval):
    """
    Work out the extent of the whole mosaic. Return an ImageInfo
    object of the output grid.
    """
    infoList = [imgInfoDict[fn] for fn in filelist]
    boundsArray = numpy.array([(i.xMin, i.xMax, i.yMin, i.yMax)
        for i in infoList])
    xMin = boundsArray[:, 0].min()
    xMax = boundsArray[:, 1].max()
    yMin = boundsArray[:, 2].min()
    yMax = boundsArray[:, 3].max()

    firstImgInfo = imgInfoDict[filelist[0]]
    outImgInfo = structures.ImageInfo(None)
    outImgInfo.projection = firstImgInfo.projection
    (xRes, yRes) = (firstImgInfo.xRes, firstImgInfo.yRes)
    outImgInfo.ncols = int(round(((xMax - xMin) / xRes)))
    outImgInfo.nrows = int(round(((yMax - yMin) / yRes)))
    outImgInfo.transform = (xMin, xRes, 0.0, yMax, 0.0, -yRes)
    outImgInfo.dataType = firstImgInfo.dataType
    outImgInfo.numBands = firstImgInfo.numBands
    outImgInfo.nullVal = firstImgInfo.nullVal
    if nullval is not None:
        outImgInfo.nullVal = nullval
    return outImgInfo


def makeOutputBlockList(outImgInfo, blocksize):
    """
    Given a pixel grid of the whole extent, divide it up into blocks.
    Return a list of BlockSpec objects.
    """
    # Divide this up into blocks
    # Should do something to avoid tiny blocks on the right and bottom edges...
    (nrows, ncols) = (outImgInfo.nrows, outImgInfo.ncols)
    blockList = []
    top = 0
    while top < nrows:
        ysize = min(blocksize, (nrows - top))
        left = 0
        while left < ncols:
            xsize = min(blocksize, (ncols - left))
            block = structures.BlockSpec(top, left, xsize, ysize)
            blockList.append(block)
            left += xsize
        top += ysize
    return blockList


def makeImgInfoDict(filelist, numthreads):
    """
    Create ImageInfo objects for all the given input files.
    Store these in a dictionary, keyed by their filenames.
    """
    imgInfoDict = {}
    for filename in filelist:
        imgInfoDict[filename] = structures.ImageInfo(filename)
    return imgInfoDict


def findInputsPerBlock(blockList, outGeoTransform, filelist, imgInfoDict):
    """
    For every block, work out which input files intersect with it,
    and the bounds of that block, in each file's pixel coordinate system.
    """
    blockListWithInputs = []
    filesForBlock = {}
    for block in blockList:
        blockWithInputs = structures.BlockSpecWithInputs(block)

        for filename in filelist:
            imginfo = imgInfoDict[filename]
            (fileLeft, fileTop, fileRight, fileBottom) = (
                block.transformToFilePixelCoords(outGeoTransform, imginfo))
            intersects = ((fileRight + 1) >= 0 and (fileBottom + 1) >= 0 and
                fileLeft <= imginfo.ncols and fileTop <= imginfo.nrows)

            if intersects:
                xs = fileRight - fileLeft
                ys = fileBottom - fileTop
                inblock = structures.BlockSpec(fileTop, fileLeft, xs, ys)
                blockWithInputs.add(filename, inblock)

                if block not in filesForBlock:
                    filesForBlock[block] = []
                filesForBlock[block].append(filename)

        if len(blockWithInputs.infilelist) > 0:
            blockListWithInputs.append(blockWithInputs)

    return (blockListWithInputs, filesForBlock)


def makeBlockReadingList(blockListWithInputs):
    """
    Make a single list of all the blocks to be read. This is returned as
    a list of BlockReadingSpec objects
    """
    blockReadingList = []
    for blockWithInputs in blockListWithInputs:
        outblock = blockWithInputs.outblock
        n = len(blockWithInputs.infilelist)
        for i in range(n):
            filename = blockWithInputs.infilelist[i]
            inblock = blockWithInputs.inblocklist[i]
            blockInfo = structures.BlockReadingSpec(outblock, filename,
                    inblock)
            blockReadingList.append(blockInfo)
    return blockReadingList


def divideBlocksByThread(blockReadingList, numthreads):
    """
    Divide up the given blockReadingList into several such lists, one
    per thread. Return a list of these sub-lists.
    """
    blocksPerThread = []
    for i in range(numthreads):
        sublist = blockReadingList[i::numthreads]
        blocksPerThread.append(sublist)
    return blocksPerThread


def getInputsForBlock(blockCache, outblock, filesForBlock):
    """
    Search the block cache for all the expected inputs for the current
    block. If all are found, return a list of them. If any are still
    missing from the cache, then just return None.
    """
    allInputsForBlock = []
    i = 0
    shp = None
    missing = False
    filelist = filesForBlock[outblock]
    numFiles = len(filelist)
    while i < numFiles and not missing:
        filename = filelist[i]
        k = blockCache.makeKey(filename, outblock)
        if k in blockCache.cache:
            (blockSpec, arr) = blockCache.cache[k]
            # Check on array shape. They must all be the same shape
            if shp is None:
                shp = arr.shape
            if arr.shape != shp:
                msg = ("Block array mismatch at block {}\n".format(
                       blockSpec) +
                       "{}!={}\n{}".format(arr.shape, shp, filelist)
                       )
                raise ValueError(msg)

            allInputsForBlock.append(arr)
            i += 1
        else:
            missing = True
            allInputsForBlock = None

    return allInputsForBlock


def openOutfile(outfile, driver, outImgInfo, creationoptions):
    """
    Open the output file
    """
    (nrows, ncols) = (outImgInfo.nrows, outImgInfo.ncols)
    numBands = outImgInfo.numBands
    datatype = outImgInfo.dataType
    if creationoptions is None:
        creationoptions = defaultCreationOptions[driver]
    drvr = gdal.GetDriverByName(driver)
    if drvr is None:
        msg = "Driver {} not supported in this version of GDAL".format(driver)
        raise ValueError(msg)

    if os.path.exists(outfile):
        drvr.Delete(outfile)
    ds = drvr.Create(outfile, ncols, nrows, numBands, datatype,
        creationoptions)
    return ds


def mergeInputs(allInputsForBlock, outNullVal):
    """
    Given a list of input arrays, merge to produce the final
    output array. Ordering is important, the last non-null
    value is the one used.
    """
    numInputs = len(allInputsForBlock)
    outArr = allInputsForBlock[0]
    for i in range(1, numInputs):
        arr = allInputsForBlock[i]
        nonNull = (arr != outNullVal)
        outArr[nonNull] = arr[nonNull]
    return outArr


def makeOutImgInfo(inImgInfo, outgrid, nullval):
    """
    Create an ImageInfo for the output file, based on one of the
    input files, and information from the outgrid and the nullval.
    """
    outImgInfo = structures.ImageInfo(None)
    (outImgInfo.nrows, outImgInfo.ncols) = outgrid.getDimensions()
    outImgInfo.numBands = inImgInfo.numBands
    outImgInfo.transform = inImgInfo.transform
    outImgInfo.projection = inImgInfo.projection
    outImgInfo.dataType = inImgInfo.dataType
    outImgInfo.nullVal = inImgInfo.nullVal
    if nullval is not None:
        outImgInfo.nullVal = nullval
    return outImgInfo


if __name__ == "__main__":
    mainCmd()
