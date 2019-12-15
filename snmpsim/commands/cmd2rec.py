#
# This file is part of snmpsim software.
#
# Copyright (c) 2010-2019, Ilya Etingof <etingof@gmail.com>
# License: http://snmplabs.com/snmpsim/license.html
#
# SNMP Snapshot Data Recorder
#
import argparse
import functools
import os
import socket
import sys
import time
import traceback

from pyasn1 import debug as pyasn1_debug
from pyasn1.type import univ
from pysnmp import debug as pysnmp_debug
from pysnmp.carrier.asyncore.dgram import udp
from pysnmp.carrier.asyncore.dgram import udp6
from pysnmp.entity import engine, config
from pysnmp.entity.rfc3413 import cmdgen
from pysnmp.error import PySnmpError
from pysnmp.proto import rfc1902
from pysnmp.proto import rfc1905
from pysnmp.smi import compiler
from pysnmp.smi import view
from pysnmp.smi.rfc1902 import ObjectIdentity

from snmpsim import confdir
from snmpsim import error
from snmpsim import log
from snmpsim import utils
from snmpsim.record import dump
from snmpsim.record import mvc
from snmpsim.record import sap
from snmpsim.record import snmprec
from snmpsim.record import walk

AUTH_PROTOCOLS = {
    'MD5': config.usmHMACMD5AuthProtocol,
    'SHA': config.usmHMACSHAAuthProtocol,
    'SHA224': config.usmHMAC128SHA224AuthProtocol,
    'SHA256': config.usmHMAC192SHA256AuthProtocol,
    'SHA384': config.usmHMAC256SHA384AuthProtocol,
    'SHA512': config.usmHMAC384SHA512AuthProtocol,
    'NONE': config.usmNoAuthProtocol
}

PRIV_PROTOCOLS = {
    'DES': config.usmDESPrivProtocol,
    '3DES': config.usm3DESEDEPrivProtocol,
    'AES': config.usmAesCfb128Protocol,
    'AES128': config.usmAesCfb128Protocol,
    'AES192': config.usmAesCfb192Protocol,
    'AES192BLMT': config.usmAesBlumenthalCfb192Protocol,
    'AES256': config.usmAesCfb256Protocol,
    'AES256BLMT': config.usmAesBlumenthalCfb256Protocol,
    'NONE': config.usmNoPrivProtocol
}

VERSION_MAP = {
    '1': 0,
    '2c': 1,
    '3': 3
}

RECORD_TYPES = {
    dump.DumpRecord.ext: dump.DumpRecord(),
    mvc.MvcRecord.ext: mvc.MvcRecord(),
    sap.SapRecord.ext: sap.SapRecord(),
    walk.WalkRecord.ext: walk.WalkRecord(),
    snmprec.SnmprecRecord.ext: snmprec.SnmprecRecord(),
    snmprec.CompressedSnmprecRecord.ext: snmprec.CompressedSnmprecRecord()
}


class SnmprecRecordMixIn(object):

    def formatValue(self, oid, value, **context):
        textOid, textTag, textValue = snmprec.SnmprecRecord.formatValue(
            self, oid, value
        )

        # invoke variation module
        if context['variationModule']:
            plainOid, plainTag, plainValue = snmprec.SnmprecRecord.formatValue(
                self, oid, value, nohex=True)

            if plainTag != textTag:
                context['hextag'], context['hexvalue'] = textTag, textValue

            else:
                textTag, textValue = plainTag, plainValue

            handler = context['variationModule']['record']

            textOid, textTag, textValue = handler(
                textOid, textTag, textValue, **context)

        elif 'stopFlag' in context and context['stopFlag']:
            raise error.NoDataNotification()

        return textOid, textTag, textValue


class SnmprecRecord(SnmprecRecordMixIn, snmprec.SnmprecRecord):
    pass


RECORD_TYPES[SnmprecRecord.ext] = SnmprecRecord()


class CompressedSnmprecRecord(
        SnmprecRecordMixIn, snmprec.CompressedSnmprecRecord):
    pass


RECORD_TYPES[CompressedSnmprecRecord.ext] = CompressedSnmprecRecord()

DESCRIPTION = ('SNMP simulation data recorder. Pull simulation data from '
               'SNMP agent')


def _parse_endpoint(arg, ipv6=False):
    address = arg

    # IPv6 notation
    if ipv6 and address.startswith('['):
        address = address.replace('[', '').replace(']', '')

    try:
        if ':' in address:
            address, port = address.split(':', 1)
            port = int(port)

        else:
            port = 161

    except Exception as exc:
        raise error.SnmpsimError(
            'Malformed network endpoint address %s: %s' % (arg, exc))

    try:
        address, port = socket.getaddrinfo(
            address, port,
            socket.AF_INET6 if ipv6 else socket.AF_INET,
            socket.SOCK_DGRAM,
            socket.IPPROTO_UDP)[0][4][:2]

    except socket.gaierror as exc:
        raise error.SnmpsimError(
            'Unknown hostname %s: %s' % (address, exc))

    return address, port


def _parse_mib_object(arg, last=False):
    if '::' in arg:
        return ObjectIdentity(*arg.split('::', 1), last=last)

    else:
        return univ.ObjectIdentifier(arg)


def main():
    variation_module = None

    parser = argparse.ArgumentParser(description=DESCRIPTION)

    parser.add_argument(
        '-v', '--version', action='version',
        version=utils.TITLE)

    parser.add_argument(
        '--quiet', action='store_true',
        help='Do not print out informational messages')

    parser.add_argument(
        '--debug', choices=pysnmp_debug.flagMap,
        action='append', type=str, default=[],
        help='Enable one or more categories of SNMP debugging.')

    parser.add_argument(
        '--debug-asn1', choices=pyasn1_debug.FLAG_MAP,
        action='append', type=str, default=[],
        help='Enable one or more categories of ASN.1 debugging.')

    parser.add_argument(
        '--logging-method', type=lambda x: x.split(':'),
        metavar='=<%s[:args]>]' % '|'.join(log.METHODS_MAP),
        default='stderr', help='Logging method.')

    parser.add_argument(
        '--log-level', choices=log.LEVELS_MAP,
        type=str, default='info', help='Logging level.')

    v1arch_group = parser.add_argument_group('SNMPv1/v2c parameters')

    v1arch_group.add_argument(
        '--protocol-version', choices=['1', '2c'],
        default='2c', help='SNMPv1/v2c protocol version')

    v1arch_group.add_argument(
        '--community', type=str, default='public',
        help='SNMP community name')

    v3arch_group = parser.add_argument_group('SNMPv3 parameters')

    v3arch_group.add_argument(
        '--v3-user', type=str,
        help='SNMPv3 USM user (security) name')

    v3arch_group.add_argument(
        '--v3-auth-key', type=str,
        help='SNMPv3 USM authentication key (must be > 8 chars)')

    v3arch_group.add_argument(
        '--v3-auth-proto', choices=AUTH_PROTOCOLS, default='NONE',
        help='SNMPv3 USM authentication protocol')

    v3arch_group.add_argument(
        '--v3-priv-key', type=str,
        help='SNMPv3 USM privacy (encryption) key (must be > 8 chars)')

    v3arch_group.add_argument(
        '--v3-priv-proto', choices=PRIV_PROTOCOLS, default='NONE',
        help='SNMPv3 USM privacy (encryption) protocol')

    v3arch_group.add_argument(
        '--v3-context-engine-id',
        type=lambda x: univ.OctetString(hexValue=x[2:]),
        help='SNMPv3 context engine ID')

    v3arch_group.add_argument(
        '--v3-context-name', type=str, default='',
        help='SNMPv3 context engine ID')

    parser.add_argument(
        '--use-getbulk', action='store_true',
        help='Use SNMP GETBULK PDU for mass SNMP managed objects retrieval')

    parser.add_argument(
        '--getbulk-repetitions', type=int, default=25,
        help='Use SNMP GETBULK PDU for mass SNMP managed objects retrieval')

    endpoint_group = parser.add_mutually_exclusive_group(required=True)

    endpoint_group.add_argument(
        '--agent-udpv4-endpoint', type=_parse_endpoint,
        metavar='<[X.X.X.X]:NNNNN>',
        help='SNMP agent UDP/IPv4 address to pull simulation data '
             'from (name:port)')

    endpoint_group.add_argument(
        '--agent-udpv6-endpoint',
        type=functools.partial(_parse_endpoint, ipv6=True),
        metavar='<[X:X:..X]:NNNNN>',
        help='SNMP agent UDP/IPv6 address to pull simulation data '
             'from ([name]:port)')

    parser.add_argument(
        '--timeout', type=int, default=3,
        help='SNMP command response timeout (in seconds)')

    parser.add_argument(
        '--retries', type=int, default=3,
        help='SNMP command retries')

    parser.add_argument(
        '--start-object', metavar='<MIB::Object|OID>', type=_parse_mib_object,
        default=univ.ObjectIdentifier('1.3.6'),
        help='Drop all simulation data records prior to this OID specified '
             'as MIB object (MIB::Object) or OID (1.3.6.)')

    parser.add_argument(
        '--stop-object', metavar='<MIB::Object|OID>',
        type=functools.partial(_parse_mib_object, last=True),
        help='Drop all simulation data records after this OID specified '
             'as MIB object (MIB::Object) or OID (1.3.6.)')

    parser.add_argument(
        '--mib-source', dest='mib_sources', metavar='<URI|PATH>',
        action='append', type=str,
        default=['http://mibs.snmplabs.com/asn1/@mib@'],
        help='One or more URIs pointing to a collection of ASN.1 MIB files.'
             'Optional "@mib@" token gets replaced with desired MIB module '
             'name during MIB search.')

    parser.add_argument(
        '--destination-record-type', choices=RECORD_TYPES, default='snmprec',
        help='Produce simulation data with record of this type')

    parser.add_argument(
        '--output-file', metavar='<FILE>', type=str,
        help='SNMP simulation data file to write records to')

    parser.add_argument(
        '--continue-on-errors', metavar='<tolerance-level>',
        type=int, default=0,
        help='Keep on pulling SNMP data even if intermittent errors occur')

    variation_group = parser.add_argument_group(
        'Simulation data variation options')

    parser.add_argument(
        '--variation-modules-dir', action='append', type=str,
        help='Search variation module by this path')

    variation_group.add_argument(
        '--variation-module', type=str,
        help='Pass gathered simulation data through this variation module')

    variation_group.add_argument(
        '--variation-module-options', type=str, default='',
        help='Variation module options')

    args = parser.parse_args()

    if args.debug:
        pysnmp_debug.setLogger(pysnmp_debug.Debug(*args.debug))

    if args.debug_asn1:
        pyasn1_debug.setLogger(pyasn1_debug.Debug(*args.debug_asn1))

    if args.output_file:
        ext = os.path.extsep + RECORD_TYPES[args.destination_record_type].ext

        if not args.output_file.endswith(ext):
            args.output_file += ext

        args.output_file = RECORD_TYPES[args.destination_record_type].open(
            args.output_file, 'wb')

    else:
        args.output_file = sys.stdout

        if sys.version_info >= (3, 0, 0):
            # binary mode write
            args.output_file = sys.stdout.buffer

        elif sys.platform == "win32":
            import msvcrt

            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

    # Catch missing params

    if args.protocol_version == '3':
        if not args.v3_user:
            sys.stderr.write('ERROR: --v3-user is missing\r\n')
            parser.print_usage(sys.stderr)
            return 1

        if args.v3_priv_key and not args.v3_auth_key:
            sys.stderr.write('ERROR: --v3-auth-key is missing\r\n')
            parser.print_usage(sys.stderr)
            return 1

        if AUTH_PROTOCOLS[args.v3_auth_proto] == config.usmNoAuthProtocol:
            if args.v3_auth_key:
                args.v3_auth_proto = 'MD5'

        else:
            if not args.v3_auth_key:
                sys.stderr.write('ERROR: --v3-auth-key is missing\r\n')
                parser.print_usage(sys.stderr)
                return 1

        if PRIV_PROTOCOLS[args.v3_priv_proto] == config.usmNoPrivProtocol:
            if args.v3_priv_key:
                args.v3_priv_proto = 'DES'

        else:
            if not args.v3_priv_key:
                sys.stderr.write('ERROR: --v3-priv-key is missing\r\n')
                parser.print_usage(sys.stderr)
                return 1

    try:
        log.setLogger(__name__, *args.logging_method, force=True)

        if args.log_level:
            log.setLevel(args.log_level)

    except error.SnmpsimError as exc:
        sys.stderr.write('%s\r\n' % exc)
        parser.print_usage(sys.stderr)
        return 1

    if args.use_getbulk and args.protocol_version == '1':
        log.info('will be using GETNEXT with SNMPv1!')
        args.use_getbulk = False

    # Load variation module

    if args.variation_module:

        for variation_modules_dir in (
                args.variation_modules_dir or confdir.variation):
            log.info(
                'Scanning "%s" directory for variation '
                'modules...' % variation_modules_dir)

            if not os.path.exists(variation_modules_dir):
                log.info('Directory "%s" does not exist' % variation_modules_dir)
                continue

            mod = os.path.join(variation_modules_dir, args.variation_module + '.py')
            if not os.path.exists(mod):
                log.info('Variation module "%s" not found' % mod)
                continue

            ctx = {'path': mod, 'moduleContext': {}}

            try:
                if sys.version_info[0] > 2:
                    exec(compile(open(mod).read(), mod, 'exec'), ctx)

                else:
                    execfile(mod, ctx)

            except Exception as exc:
                log.error('Variation module "%s" execution failure: '
                          '%s' % (mod, exc))
                return 1

            else:
                variation_module = ctx
                log.info('Variation module "%s" loaded' % args.variation_module)
                break

        else:
            log.error('variation module "%s" not found' % args.variation_module)
            return 1

    # SNMP configuration

    snmp_engine = engine.SnmpEngine()

    if args.protocol_version == '3':

        if args.v3_priv_key is None and args.v3_auth_key is None:
            secLevel = 'noAuthNoPriv'

        elif args.v3_priv_key is None:
            secLevel = 'authNoPriv'

        else:
            secLevel = 'authPriv'

        config.addV3User(
            snmp_engine, args.v3_user,
            AUTH_PROTOCOLS[args.v3_auth_proto], args.v3_auth_key,
            PRIV_PROTOCOLS[args.v3_priv_proto], args.v3_priv_key)

        log.info(
            'SNMP version 3, Context EngineID: %s Context name: %s, SecurityName: %s, '
            'SecurityLevel: %s, Authentication key/protocol: %s/%s, Encryption '
            '(privacy) key/protocol: '
            '%s/%s' % (
                args.v3_context_engine_id and args.v3_context_engine_id.prettyPrint() or '<default>',
                args.v3_context_name and args.v3_context_name.prettyPrint() or '<default>', args.v3_user,
                secLevel, args.v3_auth_key is None and '<NONE>' or args.v3_auth_key,
                args.v3_auth_proto,
                args.v3_priv_key is None and '<NONE>' or args.v3_priv_key, args.v3_priv_proto))

    else:

        args.v3_user = 'agt'
        secLevel = 'noAuthNoPriv'

        config.addV1System(snmp_engine, args.v3_user, args.community)

        log.info(
            'SNMP version %s, Community name: '
            '%s' % (args.protocol_version, args.community))

    config.addTargetParams(
        snmp_engine, 'pms', args.v3_user, secLevel, VERSION_MAP[args.protocol_version])

    if args.agent_udpv6_endpoint:
        config.addSocketTransport(
            snmp_engine, udp6.domainName,
            udp6.Udp6SocketTransport().openClientMode())

        config.addTargetAddr(
            snmp_engine, 'tgt', udp6.domainName, args.agent_udpv6_endpoint, 'pms',
            args.timeout * 100, args.retries)

        log.info('Querying UDP/IPv6 agent at [%s]:%s' % args.agent_udpv6_endpoint)

    elif args.agent_udpv4_endpoint:
        config.addSocketTransport(
            snmp_engine, udp.domainName,
            udp.UdpSocketTransport().openClientMode())

        config.addTargetAddr(
            snmp_engine, 'tgt', udp.domainName, args.agent_udpv4_endpoint, 'pms',
            args.timeout * 100, args.retries)

        log.info('Querying UDP/IPv4 agent at %s:%s' % args.agent_udpv4_endpoint)

    log.info('Agent response timeout: %d secs, retries: '
             '%s' % (args.timeout, args.retries))

    if (isinstance(args.start_object, ObjectIdentity) or
            isinstance(args.stop_object, ObjectIdentity)):

        compiler.addMibCompiler(
            snmp_engine.getMibBuilder(), sources=args.mib_sources)

        mibViewController = view.MibViewController(snmp_engine.getMibBuilder())

        try:
            if isinstance(args.start_object, ObjectIdentity):
                args.start_object.resolveWithMib(mibViewController)

            if isinstance(args.stop_object, ObjectIdentity):
                args.stop_object.resolveWithMib(mibViewController)

        except PySnmpError as exc:
            sys.stderr.write('ERROR: %s\r\n' % exc)
            return 1

    # Variation module initialization

    if variation_module:
        log.info('Initializing variation module...')

        for x in ('init', 'record', 'shutdown'):
            if x not in variation_module:
                log.error('missing "%s" handler at variation module '
                          '"%s"' % (x, args.variation_module))
                return 1

        try:
            handler = variation_module['init']

            handler(snmpEngine=snmp_engine, options=args.variation_module_options,
                    mode='recording', startOID=args.start_object, stopOID=args.stop_object)

        except Exception as exc:
            log.error(
                'Variation module "%s" initialization FAILED: '
                '%s' % (args.variation_module, exc))

        else:
            log.info(
                'Variation module "%s" initialization OK' % args.variation_module)

    data_file_handler = RECORD_TYPES[args.destination_record_type]


    # SNMP worker

    def cbFun(snmpEngine, sendRequestHandle, errorIndication,
              errorStatus, errorIndex, varBindTable, cbCtx):

        if errorIndication and not cbCtx['retries']:
            cbCtx['errors'] += 1
            log.error('SNMP Engine error: %s' % errorIndication)
            return

        # SNMPv1 response may contain noSuchName error *and* SNMPv2c exception,
        # so we ignore noSuchName error here
        if errorStatus and errorStatus != 2 or errorIndication:
            log.error(
                'Remote SNMP error %s' % (
                        errorIndication or errorStatus.prettyPrint()))

            if cbCtx['retries']:
                try:
                    nextOID = varBindTable[-1][0][0]

                except IndexError:
                    nextOID = cbCtx['lastOID']

                else:
                    log.error('Failed OID: %s' % nextOID)

                # fuzzy logic of walking a broken OID
                if len(nextOID) < 4:
                    pass

                elif (args.continue_on_errors - cbCtx['retries']) * 10 / args.continue_on_errors > 5:
                    nextOID = nextOID[:-2] + (nextOID[-2] + 1,)

                elif nextOID[-1]:
                    nextOID = nextOID[:-1] + (nextOID[-1] + 1,)

                else:
                    nextOID = nextOID[:-2] + (nextOID[-2] + 1, 0)

                cbCtx['retries'] -= 1
                cbCtx['lastOID'] = nextOID

                log.info(
                    'Retrying with OID %s (%s retries left)'
                    '...' % (nextOID, cbCtx['retries']))

                # initiate another SNMP walk iteration
                if args.use_getbulk:
                    cmdGen.sendVarBinds(
                        snmpEngine,
                        'tgt',
                        args.v3_context_engine_id, args.v3_context_name,
                        0, args.getbulk_repetitions,
                        [(nextOID, None)],
                        cbFun, cbCtx)

                else:
                    cmdGen.sendVarBinds(
                        snmpEngine,
                        'tgt',
                        args.v3_context_engine_id, args.v3_context_name,
                        [(nextOID, None)],
                        cbFun, cbCtx)

            cbCtx['errors'] += 1

            return

        if args.continue_on_errors != cbCtx['retries']:
            cbCtx['retries'] += 1

        if varBindTable and varBindTable[-1] and varBindTable[-1][0]:
            cbCtx['lastOID'] = varBindTable[-1][0][0]

        stop_flag = False

        # Walk var-binds
        for varBindRow in varBindTable:
            for oid, value in varBindRow:

                # EOM
                if args.stop_object and oid >= args.stop_object:
                    stop_flag = True  # stop on out of range condition

                elif (value is None or
                          value.tagSet in (rfc1905.NoSuchObject.tagSet,
                                           rfc1905.NoSuchInstance.tagSet,
                                           rfc1905.EndOfMibView.tagSet)):
                    stop_flag = True

                # remove value enumeration
                if value.tagSet == rfc1902.Integer32.tagSet:
                    value = rfc1902.Integer32(value)

                if value.tagSet == rfc1902.Unsigned32.tagSet:
                    value = rfc1902.Unsigned32(value)

                if value.tagSet == rfc1902.Bits.tagSet:
                    value = rfc1902.OctetString(value)

                # Build .snmprec record

                context = {
                    'origOid': oid,
                    'origValue': value,
                    'count': cbCtx['count'],
                    'total': cbCtx['total'],
                    'iteration': cbCtx['iteration'],
                    'reqTime': cbCtx['reqTime'],
                    'args.start_object': args.start_object,
                    'stopOID': args.stop_object,
                    'stopFlag': stop_flag,
                    'variationModule': variation_module
                }

                try:
                    line = data_file_handler.format(oid, value, **context)

                except error.MoreDataNotification as exc:
                    cbCtx['count'] = 0
                    cbCtx['iteration'] += 1

                    moreDataNotification = exc

                    if 'period' in moreDataNotification:
                        log.info(
                            '%s OIDs dumped, waiting %.2f sec(s)'
                            '...' % (cbCtx['total'],
                                     moreDataNotification['period']))

                        time.sleep(moreDataNotification['period'])

                    # initiate another SNMP walk iteration
                    if args.use_getbulk:
                        cmdGen.sendVarBinds(
                            snmpEngine,
                            'tgt',
                            args.v3_context_engine_id, args.v3_context_name,
                            0, args.getbulk_repetitions,
                            [(args.start_object, None)],
                            cbFun, cbCtx)

                    else:
                        cmdGen.sendVarBinds(
                            snmpEngine,
                            'tgt',
                            args.v3_context_engine_id, args.v3_context_name,
                            [(args.start_object, None)],
                            cbFun, cbCtx)

                    stop_flag = True  # stop current iteration

                except error.NoDataNotification:
                    pass

                except error.SnmpsimError as exc:
                    log.error(exc)
                    continue

                else:
                    args.output_file.write(line)

                    cbCtx['count'] += 1
                    cbCtx['total'] += 1

                    if cbCtx['count'] % 100 == 0:
                        log.info('OIDs dumped: %s/%s' % (
                            cbCtx['iteration'], cbCtx['count']))

        # Next request time
        cbCtx['reqTime'] = time.time()

        # Continue walking
        return not stop_flag

    cbCtx = {
        'total': 0,
        'count': 0,
        'errors': 0,
        'iteration': 0,
        'reqTime': time.time(),
        'retries': args.continue_on_errors,
        'lastOID': args.start_object
    }

    if args.use_getbulk:
        cmdGen = cmdgen.BulkCommandGenerator()

        cmdGen.sendVarBinds(
            snmp_engine,
            'tgt',
            args.v3_context_engine_id, args.v3_context_name,
            0, args.getbulk_repetitions,
            [(args.start_object, rfc1902.Null(''))],
            cbFun, cbCtx)

    else:
        cmdGen = cmdgen.NextCommandGenerator()

        cmdGen.sendVarBinds(
            snmp_engine,
            'tgt',
            args.v3_context_engine_id, args.v3_context_name,
            [(args.start_object, rfc1902.Null(''))],
            cbFun, cbCtx)

    log.info(
        'Sending initial %s request for %s (stop at %s)'
        '....' % (args.use_getbulk and 'GETBULK' or 'GETNEXT',
                  args.start_object, args.stop_object or '<end-of-mib>'))

    started = time.time()

    try:
        snmp_engine.transportDispatcher.runDispatcher()

    except KeyboardInterrupt:
        log.info('Shutting down process...')

    finally:
        if variation_module:
            log.info('Shutting down variation module '
                     '%s...' % args.variation_module)

            try:
                handler = variation_module['shutdown']

                handler(snmpEngine=snmp_engine,
                        options=args.variation_module_options,
                        mode='recording')

            except Exception as exc:
                log.error(
                    'Variation module %s shutdown FAILED: '
                    '%s' % (args.variation_module, exc))

            else:
                log.info(
                    'Variation module %s shutdown OK' % args.variation_module)

        snmp_engine.transportDispatcher.closeDispatcher()

        started = time.time() - started

        cbCtx['total'] += cbCtx['count']

        log.info(
            'OIDs dumped: %s, elapsed: %.2f sec, rate: %.2f OIDs/sec, errors: '
            '%d' % (cbCtx['total'], started,
                    started and cbCtx['count'] // started or 0,
                    cbCtx['errors']))

        args.output_file.flush()
        args.output_file.close()

        return 0


if __name__ == '__main__':
    try:
        rc = main()

    except KeyboardInterrupt:
        sys.stderr.write('shutting down process...')
        rc = 0

    except Exception as exc:
        sys.stderr.write('process terminated: %s' % exc)

        for line in traceback.format_exception(*sys.exc_info()):
            sys.stderr.write(line.replace('\n', ';'))
        rc = 1

    sys.exit(rc)