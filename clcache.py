#!/usr/bin/env python
#
# clcache.py - a compiler cache for Microsoft Visual Studio
#
# Copyright (c) 2010, 2011, froglogic GmbH <raabe@froglogic.com>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the <organization> nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL <COPYRIGHT HOLDER> BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
from filelock import FileLock
import hashlib
import json
import os
from shutil import copyfile, rmtree
import subprocess
from subprocess import Popen, PIPE, STDOUT
import sys
import codecs

def cacheLock(cache):
    lock = FileLock("x", timeout=2)
    lock.lockfile = os.path.join(cache.cacheDirectory(), "cache.lock")
    return lock

class ObjectCache:
    def __init__(self):
        try:
            self.dir = os.environ["CLCACHE_DIR"]
        except KeyError:
            self.dir = os.path.join(os.path.expanduser("~"), "clcache")
        if not os.path.exists(self.dir):
            os.makedirs(self.dir)

    def cacheDirectory(self):
        return self.dir

    def clean(self, stats, maximumSize):
        currentSize = stats.currentCacheSize()
        if currentSize < maximumSize:
            return

        objects = [os.path.join(root, "object")
                   for root, folder, files in os.walk(self.dir)
                   if "object" in files]

        objectInfos = [(os.stat(fn), fn) for fn in objects]
        objectInfos.sort(key=lambda t: t[0].st_atime, reverse=True)

        for stat, fn in objectInfos:
            rmtree(os.path.split(fn)[0])
            currentSize -= stat.st_size
            if currentSize < maximumSize:
                break

        stats.setCacheSize(currentSize)

    def computeKey(self, compilerBinary, commandLine):
        ppcmd = [compilerBinary, "/EP"]
        ppcmd += [arg for arg in commandLine[1:] if not arg in ("-c", "/c")]
        preprocessor = Popen(ppcmd, stdout=PIPE, stderr=open(os.devnull, 'w'))
        preprocessedSourceCode = preprocessor.communicate()[0]

        normalizedCmdLine = self._normalizedCommandLine(commandLine[1:])

        stat = os.stat(compilerBinary)
        sha = hashlib.sha1()
        sha.update(str(stat.st_mtime))
        sha.update(str(stat.st_size))
        sha.update(' '.join(normalizedCmdLine))
        sha.update(preprocessedSourceCode)
        return sha.hexdigest()

    def hasEntry(self, key):
        return os.path.exists(self.cachedObjectName(key))

    def setEntry(self, key, objectFileName, compilerOutput):
        if not os.path.exists(self._cacheEntryDir(key)):
            os.makedirs(self._cacheEntryDir(key))
        copyfile(objectFileName, self.cachedObjectName(key))
        open(self._cachedCompilerOutputName(key), 'w').write(compilerOutput)

    def cachedObjectName(self, key):
        return os.path.join(self._cacheEntryDir(key), "object")

    def cachedCompilerOutput(self, key):
        return open(self._cachedCompilerOutputName(key), 'r').read()

    def _cacheEntryDir(self, key):
        return os.path.join(self.dir, key[:2], key)

    def _cachedCompilerOutputName(self, key):
        return os.path.join(self._cacheEntryDir(key), "output.txt")

    def _normalizedCommandLine(self, cmdline):
        # Remove all arguments from the command line which only influence the
        # preprocessor; the preprocessor's output is already included into the
        # hash sum so we don't have to care about these switches in the
        # command line as well.
        _argsToStrip = ("AI", "C", "E", "P", "FI", "u", "X",
                        "FU", "D", "EP", "Fx", "U", "I")

        # Also remove the switch for specifying the output file name; we don't
        # want two invocations which are identical except for the output file
        # name to be treated differently.
        _argsToStrip += ("Fo",)

        return [arg for arg in cmdline
                if not (arg[0] in "/-" and arg[1:].startswith(_argsToStrip))]

class PersistentJSONDict:
    def __init__(self, fileName):
        self._dirty = False
        self._dict = {}
        self._fileName = fileName
        try:
            self._dict = json.load(open(self._fileName, 'r'))
        except:
            pass

    def save(self):
        if self._dirty:
            json.dump(self._dict, open(self._fileName, 'w'))

    def __setitem__(self, key, value):
        self._dict[key] = value
        self._dirty = True

    def __getitem__(self, key):
        return self._dict[key]

    def __contains__(self, key):
        return key in self._dict


class Configuration:
    _defaultValues = { "MaximumCacheSize": 1024 * 1024 * 1000 }

    def __init__(self, objectCache):
        self._cfg = PersistentJSONDict(os.path.join(objectCache.cacheDirectory(),
                                                    "config.txt"))
        for setting, defaultValue in self._defaultValues.iteritems():
            if not setting in self._cfg:
                self._cfg[setting] = defaultValue

    def maximumCacheSize(self):
        return self._cfg["MaximumCacheSize"]

    def setMaximumCacheSize(self, size):
        self._cfg["MaximumCacheSize"] = size

    def save(self):
        self._cfg.save()


class CacheStatistics:
    def __init__(self, objectCache):
        self._stats = PersistentJSONDict(os.path.join(objectCache.cacheDirectory(),
                                                      "stats.txt"))
        for k in ["CallsWithoutSourceFile",
                  "CallsWithMultipleSourceFiles",
                  "CallsForLinking",
                  "CacheEntries", "CacheSize",
                  "CacheHits", "CacheMisses"]:
            if not k in self._stats:
                self._stats[k] = 0

    def numCallsWithoutSourceFile(self):
        return self._stats["CallsWithoutSourceFile"]

    def registerCallWithoutSourceFile(self):
        self._stats["CallsWithoutSourceFile"] += 1

    def numCallsWithMultipleSourceFiles(self):
        return self._stats["CallsWithMultipleSourceFiles"]

    def registerCallWithMultipleSourceFiles(self):
        self._stats["CallsWithMultipleSourceFiles"] += 1

    def numCallsForLinking(self):
        return self._stats["CallsForLinking"]

    def registerCallForLinking(self):
        self._stats["CallsForLinking"] += 1

    def numCacheEntries(self):
        return self._stats["CacheEntries"]

    def registerCacheEntry(self, size):
        self._stats["CacheEntries"] += 1
        self._stats["CacheSize"] += size

    def currentCacheSize(self):
        return self._stats["CacheSize"]

    def setCacheSize(self, size):
        self._stats["CacheSize"] = size

    def numCacheHits(self):
        return self._stats["CacheHits"]

    def registerCacheHit(self):
        self._stats["CacheHits"] += 1

    def numCacheMisses(self):
        return self._stats["CacheMisses"]

    def registerCacheMiss(self):
        self._stats["CacheMisses"] += 1

    def save(self):
        self._stats.save()

class AnalysisResult:
    Ok, NoSourceFile, MultipleSourceFiles, CalledForLink = range(4)

def findCompilerBinary():
    try:
        path = os.environ["CLCACHE_CL"]
        if os.path.exists(path):
            return path
    except KeyError:
        for dir in os.environ["PATH"].split(os.pathsep):
            path = os.path.join(dir, "cl.exe")
            if os.path.exists(path):
                return path
    return None


def printTraceStatement(msg):
    if "CLCACHE_LOG" in os.environ:
        print "*** clcache.py: " + msg

def expandCommandLine(cmdline):
    ret = []

    for arg in cmdline:
        if arg[0] == '@':
            includeFile = arg[1:]
            f = codecs.open(includeFile, 'r', 'utf-16') 
            includeFileContents = f.read()
            f.close()

            includeFileTokens = includeFileContents.split()
            ret.extend(expandCommandLine(includeFileTokens))
        else:
            ret.append(arg)

    return ret

def analyzeCommandLine(cmdline):
    foundCompileOnlySwitch = False
    sourceFile = None
    outputFile = None
    for arg in cmdline[1:]:
        if arg[0] == '/' or arg[0] == '-':
            if arg[1:] == 'link':
                return AnalysisResult.CalledForLink, None, None
            elif arg[1] == 'c':
                foundCompileOnlySwitch = True
            elif arg[1:3] == 'Fo':
                outputFile = arg[3:]
            elif arg[1:3] in ('Tp', 'Tc'):
                sourceFile = arg[3:]
        elif arg[0] == '@':
            # shouldn't happen!
            return AnalysisResult.MultipleSourceFiles, None, None
        else:
            if sourceFile:
                return AnalysisResult.MultipleSourceFiles, None, None
            sourceFile = arg
    if not outputFile and sourceFile:
        srcFileName = os.path.basename(sourceFile)
        outputFile = os.path.join(os.getcwd(),
                                  os.path.splitext(srcFileName)[0] + ".obj")
    if not foundCompileOnlySwitch:
        return AnalysisResult.CalledForLink, None, None
    if sourceFile == "":
        return AnalysisResult.NoSourceFile, None, None
    return AnalysisResult.Ok, sourceFile, outputFile


def invokeRealCompiler(compilerBinary, cmdLine, captureOutput=False):
    realCmdline = [compilerBinary] + cmdLine

    returnCode = None
    output = None
    if captureOutput:
        compilerProcess = Popen(realCmdline, stdout=PIPE, stderr=STDOUT)
        output = compilerProcess.communicate()[0]
        returnCode = compilerProcess.returncode
    else:
        returnCode = subprocess.call(realCmdline)
    return returnCode, output

def printStatistics():
    cache = ObjectCache()
    stats = CacheStatistics(cache)
    cfg = Configuration(cache)
    print """clcache statistics:
  current cache dir        : %s
  cache size               : %d bytes
  maximum cache size       : %d bytes
  cache entries            : %d
  cache hits               : %d
  cache misses             : %d
  called for linking       : %d
  called w/o sources       : %d
  calls w/ multiple sources: %d""" % (
       cache.cacheDirectory(),
       stats.currentCacheSize(),
       cfg.maximumCacheSize(),
       stats.numCacheEntries(),
       stats.numCacheHits(),
       stats.numCacheMisses(),
       stats.numCallsForLinking(),
       stats.numCallsWithoutSourceFile(),
       stats.numCallsWithMultipleSourceFiles())

if len(sys.argv) == 2 and sys.argv[1] == "--help":
    print """\
clcache.py v0.1"
  --help   : show this help
  -s       : print cache statistics
  -M <size>: set maximum cache size (in bytes)
"""
    sys.exit(0)

if len(sys.argv) == 2 and sys.argv[1] == "-s":
    printStatistics()
    sys.exit(0)

if len(sys.argv) == 3 and sys.argv[1] == "-M":
    cache = ObjectCache()
    cfg = Configuration(cache)
    cfg.setMaximumCacheSize(int(sys.argv[2]))
    cfg.save()
    sys.exit(0)

compiler = findCompilerBinary()
if not compiler:
    print "Failed to locate cl.exe on PATH (and CLCACHE_CL is not set), aborting."
    sys.exit(1)

if "CLCACHE_DISABLE" in os.environ:
    sys.exit(invokeRealCompiler(compiler, sys.argv[1:])[0])
   
cmdLine = expandCommandLine(sys.argv[1:])
analysisResult, sourceFile, outputFile = analyzeCommandLine(cmdLine)

cache = ObjectCache()
stats = CacheStatistics(cache)
lock = cacheLock(cache)
if analysisResult != AnalysisResult.Ok:
    if analysisResult == AnalysisResult.NoSourceFile:
        printTraceStatement("Cannot cache invocation as %s: no source file found" % (' '.join(cmdLine)) )
        stats.registerCallWithoutSourceFile()
    elif analysisResult == AnalysisResult.MultipleSourceFiles:
        printTraceStatement("Cannot cache invocation as %s: multiple source files found" % (' '.join(cmdLine)) )
        stats.registerCallWithMultipleSourceFiles()
    elif analysisResult == AnalysisResult.CalledForLink or \
         analysisResult == AnalysisResult.NoCompileOnly:
        printTraceStatement("Cannot cache invocation as %s: called for linking" % (' '.join(cmdLine)) )
        stats.registerCallForLinking()
    stats.save()
    sys.exit(invokeRealCompiler(compiler, sys.argv[1:])[0])

cachekey = cache.computeKey(compiler, cmdLine)
if cache.hasEntry(cachekey):
    stats.registerCacheHit()
    stats.save()
    printTraceStatement("Reusing cached object for key " + cachekey + " for " +
                        "output file " + outputFile)
    copyfile(cache.cachedObjectName(cachekey), outputFile)
    sys.stdout.write(cache.cachedCompilerOutput(cachekey))
    sys.exit(0)
else:
    stats.registerCacheMiss()
    returnCode, compilerOutput = invokeRealCompiler(compiler, sys.argv[1:], captureOutput=True)
    if returnCode == 0:
        printTraceStatement("Adding file " + outputFile + " to cache using " +
                            "key " + cachekey)
        cache.setEntry(cachekey, outputFile, compilerOutput)
        stats.registerCacheEntry(os.path.getsize(outputFile))
        cfg = Configuration(cache)
        cache.clean(stats, cfg.maximumCacheSize())
    stats.save()
    sys.stdout.write(compilerOutput)
    sys.exit(returnCode)
