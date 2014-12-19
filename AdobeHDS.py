#! /usr/bin/python
# vim:ts=4:sw=4:ai:et:si:sts=4:fileencoding=utf-8

# M6 version 0.1 par k3c
import binascii
import struct
import sys
import traceback
import os
import base64 
import math
import xml.etree.ElementTree
import xml.sax
import re
from urlparse import urlparse, urlunparse
import string
import unicodedata
import Queue
import threading, thread
import time
try:
    import urllib3
    from urllib3.exceptions import HTTPError
    hasUrllib3 = True
except ImportError:
    import urllib2
    from urllib2 import HTTPError
    hasUrllib3 = False
import argparse
import json

NumWorkerThreads = None
currChunknum = None

class GetUrl(object):
    def __init__(self, url, chunknum, fragnum):
        self.url = url
        self.chunkNum = chunknum
        self.fragNum = fragnum
        self.data = None
        self.errCount = 0

QueueUrl = Queue.PriorityQueue()
QueueUrlDone = Queue.PriorityQueue()

M6Item = None

def workerRun():
    global QueueUrl, QueueUrlDone, M6Item
    while not QueueUrl.empty() and M6Item.status == 'DOWNLOADING':
        item = QueueUrl.get()[1]
        fragUrl = item.url
        try:
            item.data = M6Item.getFile(fragUrl)
            QueueUrlDone.put((item.fragNum, item))
        except HTTPError, e:
            print sys.exc_info()
            traceback.print_exc(file=sys.stdout)
            if item.errCount > 3:
                M6Item.status = 'STOPPED'
                # raise
            else:
                item.errCount += 1
                QueueUrl.put((item.fragNum, item))
        QueueUrl.task_done()
    # If we have exited the previous loop with error
    while not QueueUrl.empty():
        QueueUrl.get()

def worker(errQueue):
    error = None
    try:
        workerRun()
    except Exception as e:
        print sys.exc_info()
        traceback.print_exc(file=sys.stdout)
        error = str(e)
        M6Item.status = 'STOPPED'
        thread.interrupt_main()

    if error:
        errQueue.put(error)

def workerqdRun():
    global QueueUrlDone, M6Item, currChunknum
    currentFrag = 1
    outFile = open(M6Item.localfilename, "wb")
    while currentFrag <= M6Item.nbFragments and M6Item.status == 'DOWNLOADING':
        item = QueueUrlDone.get()[1]
        requeue = False
        if currChunknum == item.chunkNum and currentFrag == item.fragNum:
            # M6Item.verifyFragment(item.data)
            if not M6Item.decodeFragment(item.fragNum, item.data):
                M6Item.status = 'FINISHED'
            else:
                M6Item.videoFragment(item.chunkNum, item.fragNum, item.data, outFile)
                print 'Fragment', currentFrag, 'OK'
                currentFrag += 1  
        elif currChunknum == item.chunkNum:
            print 'Requeue', item.fragNum
            QueueUrlDone.put((item.fragNum, item))
            requeue = True
        QueueUrlDone.task_done()
        if requeue:
            time.sleep(1)
    outFile.close()
    # If we have exited the previous loop with error
    if currentFrag > M6Item.nbFragments:
        M6Item.status = 'COMPLETED'
    else:
        while not QueueUrlDone.empty():
            print 'Ignore fragment', QueueUrlDone.get()[1].fragNum
        M6Item.status = 'COMPLETED'

def workerqd(errQueue):
    error = None
    try:
        workerqdRun()
    except Exception as e:
        print sys.exc_info()
        error = str(e)
        traceback.print_exc(file=sys.stdout)
        M6Item.status = 'STOPPED'
        thread.interrupt_main()
        while not QueueUrlDone.empty():
            print 'Flush fragment', QueueUrlDone.get()[1].fragNum

    if error:
        errQueue.put(error)

validFilenameChars = "-_.() %s%s" % (string.ascii_letters, string.digits)

def removeDisallowedFilenameChars(filename):
    "Remove invalid filename characters" 
    filename = filename.decode('ASCII', 'ignore')
    cleanedFilename = unicodedata.normalize('NFKD', filename).encode('ASCII', 'ignore')
    cleanedFilename = cleanedFilename.replace(' ', '_')
    return ''.join(c for c in cleanedFilename if c in validFilenameChars)              

class M6(object):
    def __init__(self, url, dest = '', proxy=None, maxbitrate=10000):
        self.status = 'INIT'
        self.url = url
        self.dest = dest
        self.proxy = proxy
        self.maxbitrate = maxbitrate
        self.bitrate = 0
        self.duration = 0                        
        self.nbFragments = 0
        self.tagHeaderLen = 11
        self.prevTagSize = 4
        self.urlbootstrap = ''
        self.bootstrapInfoId = ''
        self.error = None

        if hasUrllib3:
            if self.proxy:
                httpproxy = "http://%s/" % self.proxy
                self.pm = urllib3.ProxyManager(httpproxy, num_pools=100)
            else:
                self.pm = urllib3.PoolManager(num_pools=100)
        elif self.proxy:
            proxy_handler = urllib2.ProxyHandler({'http':self.proxy})
            opener = urllib2.build_opener(proxy_handler)
            urllib2.install_opener(opener)

        if self.url:
            self.manifest = self.getManifest(self.url)
            manifestVersion = self.manifestVersion()
            if manifestVersion == 2.0:
                self.parseManifestV2()
            elif manifestVersion == 1.0:
                self.parseManifestV1()        
            else:
                self.error = "Unknown manifest version"
      
    def download(self):
        global QueueUrl, QueueUrlDone, M6Item, currChunknum
        M6Item = self
        self.status = 'DOWNLOADING'
        # self.outFile = open(self.localfilename, "wb")

        for i in range(self.nbFragments):
            fragUrl = self.urlbootstrap + 'Seg1-Frag'+str(i + 1)
            QueueUrl.put((i + 1, GetUrl(fragUrl, currChunknum, i + 1)))

        errQueue = Queue.Queue()

        t = threading.Thread(target=workerqd, args=(errQueue,))
        t.start()

        for i in range(NumWorkerThreads):
            t = threading.Thread(target=worker, args=(errQueue,))
            t.start()

        while self.status == 'DOWNLOADING':
            try:
                time.sleep(3)
            except (KeyboardInterrupt, Exception), e:
                print sys.exc_info()
                traceback.print_exc(file=sys.stdout)
                self.status = 'STOPPED'

        if self.status != 'STOPPED':
            self.status = 'COMPLETED'

        try:
            error = errQueue.get(False)
            return error
        except Exception:
            return None

    def getInfos(self):
        infos = {}
        infos['status']        = self.status
        infos['localfilename'] = self.localfilename
        infos['proxy']         = self.proxy
        infos['url']           = self.url
        infos['bitrate']       = self.bitrate
        infos['duration']      = self.duration
        infos['nbFragments']   = self.nbFragments
        infos['urlbootstrap']  = self.urlbootstrap
        infos['baseUrl']       = self.baseUrl
        infos['drmId']         = self.drmAdditionalHeaderId
        return infos

    if hasUrllib3:
        def getFile(self, url):
            headers = urllib3.make_headers(
                keep_alive=True,
                user_agent='Mozilla/5.0 (iPhone; U; CPU iPhone OS 4_3_2 like Mac OS X; en-us) AppleWebKit/533.17.9 (KHTML, like Gecko) Version/5.0.2 Mobile/8H7 Safari/653.18.5',
                accept_encoding=True)
            r = self.pm.request('GET', url, headers=headers)
            if r.status != 200:
                self.error = 'Error downloading: %s, %s' % (r.status, url)
                print self.error
            return r.data
    else:
        def getFile(self, url):
            txheaders = {'User-Agent':
                             'Mozilla/5.0 (iPhone; U; CPU iPhone OS 4_3_2 like Mac OS X; en-us) AppleWebKit/533.17.9 (KHTML, like Gecko) Version/5.0.2 Mobile/8H7 Safari/653.18.5',
                         'Keep-Alive' : '600',
                         'Connection' : 'keep-alive'
                         }
            request = urllib2.Request(url, None, txheaders)
            response = urllib2.urlopen(request)
            return response.read()

    def getManifest(self, url):
        self.status = 'GETTING MANIFEST'
        return xml.etree.ElementTree.fromstring(self.getFile(url))

    def manifestVersion(self):
        root = self.manifest
        if root.tag == '{http://ns.adobe.com/f4m/2.0}manifest':
            print "Found v2.0 F4M"
            return 2.0

        if root.tag == '{http://ns.adobe.com/f4m/1.0}manifest':
            print "Found v1.0 F4M"
            return 1.0

        print "Can't find manifest"
        return 0.0

    def parseFilename(self, url):
        # http://foodnetwork-vh.akamaihd.net/z/733/459/FOOD_TheMain_E1002_,high,highest,medium,low,lowest,_16x9.mp4.csmil/manifest.f4m?
        m = re.match(r'^https?://[^/]+?/z/\d+/\d+/(.*?),.*,(.*?\.mp4).*$', url)
        if m:
            return m.group(1) + m.group(2)

        # http://foodnetwork-vh.akamaihd.net/z/,1006/187/FOOD_ChoppedCan_E1005.mp4,.csmil/manifest.f4m?
        m = re.match(r'^https?://[^/]+?/z/,\d+/\d+/(.*?\.mp4).*$', url)
        if m:
            return m.group(1)

        urlp = urlparse(url)
        return urlp.path

    def parseManifestV2(self):
        self.status = 'PARSING MANIFEST'
        try:
            root = self.manifest

            # media
            self.media = None
            for media in root.findall('{http://ns.adobe.com/f4m/2.0}media'):
                bitrate = int(media.attrib['bitrate'])
                if bitrate > self.bitrate and bitrate <= self.maxbitrate:
                    self.bitrate = bitrate
                    suburl = media.attrib['href']
                    submanifest = self.getManifest(suburl)

            self.url = suburl
            urlp = urlparse(self.url)
            fn = os.path.basename(self.parseFilename(self.url))
            self.localfilename = os.path.splitext(fn)[0]
            if self.localfilename.endswith('.mp4'):
                self.localfilename = os.path.splitext(self.localfilename)[0]
            self.localfilename = self.localfilename + '.flv'
            self.localfilename = removeDisallowedFilenameChars(self.localfilename)
            self.localfilename = os.path.join(self.dest, self.localfilename)
            self.baseUrl = urlunparse((urlp.scheme, urlp.netloc, 
                                       os.path.dirname(urlp.path), '', '', ''))

            root = submanifest

            self.media = root.find("{http://ns.adobe.com/f4m/1.0}media")

            self.bootstrapInfoId = self.media.attrib['bootstrapInfoId']
            print xml.etree.ElementTree.tostring(self.media)
            if 'drmAdditionalHeaderId' in self.media.attrib:
                self.drmAdditionalHeaderId = self.media.attrib['drmAdditionalHeaderId']
            else:
                self.drmAdditionalHeaderId = None
            self.flvHeader = base64.b64decode(self.media.find("{http://ns.adobe.com/f4m/1.0}metadata").text)

            # Duration
            self.duration = float(root.find("{http://ns.adobe.com/f4m/1.0}duration").text)
            # nombre de fragment
            self.nbFragments = int(math.ceil(self.duration/3))
            # streamid
            self.streamid = self.media.attrib['streamId']
            # Bootstrap URL
            self.urlbootstrap = self.media.attrib["url"]
            # urlbootstrap
            self.urlbootstrap = self.baseUrl + "/" + self.urlbootstrap
        except Exception as e:
            self.error = "Not possible to parse the manifest: %s" % e
            print self.error
            traceback.print_exc()

    def parseManifestV1(self):
        self.status = 'PARSING MANIFEST'
        try:
            root = self.manifest
            # Duration
            self.duration = float(root.find("{http://ns.adobe.com/f4m/1.0}duration").text)
            # nombre de fragment
            self.nbFragments = int(math.ceil(self.duration/3))
            # streamid
            self.streamid = root.findall("{http://ns.adobe.com/f4m/1.0}media")[-1]
            # media
            self.media = None
            for media in root.findall('{http://ns.adobe.com/f4m/1.0}media'):
                bitrate = int(media.attrib['bitrate'])
                if bitrate > self.bitrate and bitrate <= self.maxbitrate:
                    self.media = media
                    self.bitrate = bitrate
                    self.bootstrapInfoId = media.attrib['bootstrapInfoId']
                    if 'drmAdditionalHeaderId' in media.attrib:
                        self.drmAdditionalHeaderId = media.attrib['drmAdditionalHeaderId']
                    else:
                        self.drmAdditionalHeaderId = None
                    self.flvHeader = base64.b64decode(media.find("{http://ns.adobe.com/f4m/1.0}metadata").text)
            # Bootstrap URL
            self.urlbootstrap = self.media.attrib["url"]
        
            urlp = urlparse(self.url)
            fn = os.path.basename(self.parseFilename(self.url))
            self.localfilename = os.path.splitext(fn)[0]
            if self.localfilename.endswith('.mp4'):
                self.localfilename = os.path.splitext(self.localfilename)[0]
            self.localfilename = self.localfilename + '.flv'
            self.localfilename = removeDisallowedFilenameChars(self.localfilename)
            self.localfilename = os.path.join(self.dest, self.localfilename)
            self.baseUrl = urlunparse((urlp.scheme, urlp.netloc, 
                                       os.path.dirname(urlp.path), '', '', ''))

            # urlbootstrap
            self.urlbootstrap = self.baseUrl + "/" + self.urlbootstrap
        except Exception as e:
            self.error = "Not possible to parse the manifest: %s" % e
            print self.error
            traceback.print_exc()

    def stop(self):
        self.status = 'STOPPED'
    
    def videoFragment(self, chunkNum, fragNum, data, fout):
        start = M6Item.videostart(chunkNum, fragNum, data)
        if fragNum == 1:
            self.videoBootstrap(fout)
        fout.write(data[start:])

    def videoBootstrap(self, fout):
        # Ajout de l'en-tête FLV
        # fout.write(binascii.a2b_hex("464c560105000000090000000012"))
        # fout.write(binascii.a2b_hex("00018700000000000000")) 
        bootstrap = "464c560105000000090000000012"
        bootstrap += "%06X" % (len(self.flvHeader),)
        bootstrap += "%06X%08X" % (0, 0)
        fout.write(binascii.a2b_hex(bootstrap))
        # Ajout de l'header du fichier
        fout.write(self.flvHeader)
        fout.write(binascii.a2b_hex("00019209"))

    def videostart(self, chunkNum, fragNum, fragData):
        """
        Trouve le debut de la video dans un fragment
        """
        start = fragData.find("mdat") + 12
        # print "start ", start
        # For all fragment (except frag1)
        if fragNum == 1:
            start += 0
        else:
            # Skip 2 FLV tags
            for dummy in range(2):
                tagLen, = struct.unpack_from(">L", fragData, start)  # Read 32 bits (big endian)
                # print 'tagLen = %X' % tagLen
                tagLen &= 0x00ffffff  # Take the last 24 bits
                # print 'tagLen2 = %X' % tagLen
                start += tagLen + self.tagHeaderLen + 4  # 11 = tag header len ; 4 = tag footer len
        return start           

    def readBoxHeader(self, data, pos=0):
        boxSize, = struct.unpack_from(">L", data, pos)  # Read 32 bits (big endian)struct.unpack_from(">L", data, pos)  # Read 32 bits (big endian)
        boxType = data[pos + 4 : pos + 8]
        if boxSize == 1:
            boxSize, = struct.unpack_from(">Q", data, pos + 8)  # Read 64 bits (big endian)
            boxSize -= 16
            pos += 16
        else:
            boxSize -= 8
            pos += 8
        if boxSize <= 0:
            boxSize = 0
        return (pos, boxType, boxSize)

    def verifyFragment(self, data):
        pos = 0
        fragLen = len(data)
        print fragLen
        while pos < fragLen:
            pos, boxType, boxSize = self.readBoxHeader(data, pos)
            if boxType == 'mdat':
                slen = len(data[pos:])
                print 'mdat %s' % (slen,)
                if boxSize and slen == boxSize:
                    return True
                else:
                    boxSize = fraglen - pos
            pos += boxSize
        return False

    def decodeFragment(self, fragNum, data):
        fragPos = 0
        fragLen = len(data)
        if not self.verifyFragment(data):
            print "Skipping fragment number", fragNum
            return False
        while fragPos < fragLen:
            fragPos, boxType, boxSize = self.readBoxHeader(data, fragPos)
            if boxType == 'mdat':
                fragLen = fragPos + boxSize
                break
            fragPos += boxSize
        while fragPos < fragLen:
            packetType = self.readInt8(data, fragPos)
            packetSize = self.readInt24(data, fragPos + 1)
            packetTS = self.readInt24(data, fragPos + 4)
            packetTS |= self.readInt8(data, fragPos + 7) << 24
            if packetTS & 0x80000000:
                packetTS &= 0x7FFFFFFF
            totalTagLen = self.tagHeaderLen + packetSize + self.prevTagSize
            # print 'decodeFragment', fragNum, packetType, packetSize, packetTS, totalTagLen
            # time.sleep(1)
            if packetType in (10, 11):
                print "This stream is encrypted with Akamai DRM. Decryption of such streams isn't currently possible with this script."
                raise Exception('Akamai DRM')
            if packetType in (40, 41):
                print "This stream is encrypted with FlashAccess DRM. Decryption of such streams isn't currently possible with this script."
                raise Exception('FlashAccess DRM')
            fragPos += totalTagLen
        return True

    def readInt8(self, data, pos):
        return ord(struct.unpack_from(">c", data, pos)[0])

    def readInt24(self, data, pos):
        return struct.unpack_from(">L", "\0" + data[pos:pos + 3], 0)[0]

def main():
    global NumWorkerThreads, currChunknum
    parser = argparse.ArgumentParser(description="Grab AdobeHDS format files")
    parser.add_argument("--proxy", dest='proxy', action='store',
                        help='HTTP Proxy to use')
    parser.add_argument("--threads", dest='threads', action='store', type=int,
                        help='number of threads to use', default=7,
                        choices=range(1, 16))
    parser.add_argument("urls", metavar='U', nargs='*',
                        help='manifest URLs to grab from')
    parser.add_argument("--outdir", dest='outdir', action='store',
                        help='output directory', default='./')
    parser.add_argument("--stack", dest='stack', action='store',
                        help='media stack JSON (from CTV)')
    parser.add_argument("--maxbitrate", dest='maxbitrate', action='store',
                        help='maximum bitrate (kbit/s) to download', type=int,
                        default=10000)
    parser.add_argument("--jsonout", dest='jsonout', action='store',
                        help='JSON output file')
    args = parser.parse_args()

    NumWorkerThreads = args.threads
    urls = args.urls
    if not urls:
        urls = []

    sections = []

    if args.stack:
        x = M6(None, dest=args.outdir, proxy=args.proxy)
        print args.stack
        stackText = x.getFile(args.stack)
        print stackText
        stack = json.loads(stackText)
        items = stack['Items']
        for item in items:
            urls.append(args.stack + "/%s/manifest.f4m" % item['Id'])

    currChunknum = 1
    error = None
    for url in urls:
        st = time.time()
        x = M6(url, dest=args.outdir, proxy=args.proxy,
               maxbitrate=args.maxbitrate)
        if x.error:
            error = x.error
            break
        sections.append(os.path.split(x.localfilename)[1])
        infos = x.getInfos()
        for item in infos.items():
            print item[0]+' : '+str(item[1])
        error = x.download()
        print 'Download time:', time.time() - st
        currChunknum += 1
        if x.status == 'STOPPED' and not error:
            error = "Download stopped"
        if error:
            break

    if args.jsonout:
        files = { 'segments' : sections }
        if error:
            files['error'] = error
        with open(args.jsonout, "w") as f:
            f.write(json.dumps(files))

    if error:
        sys.exit(1)

    sys.exit(0)

if __name__ == "__main__":
    main()
