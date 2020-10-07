"""
Enable users to merge YAML/Compatible files.

Due to the complexities of merging, users are given deep control over the merge
operation via both default behaviors as well as per YAML Path behaviors.

Copyright 2020 William W. Kimball, Jr. MBA MSIS
"""
import sys
import argparse
from os import access, R_OK
from os.path import isfile, exists
from types import GeneratorType
from typing import Any

from yamlpath.merger.enums import (
    AnchorConflictResolutions,
    AoHMergeOpts,
    ArrayMergeOpts,
    HashMergeOpts,
    OutputDocTypes,
)
from yamlpath.func import get_yaml_data, get_yaml_editor
from yamlpath.merger.exceptions import MergeException
from yamlpath.merger import Merger, MergerConfig
from yamlpath.exceptions import YAMLPathException

from yamlpath.wrappers import ConsolePrinter

# Implied Constants
MY_VERSION = "0.1.0"

def processcli():
    """Process command-line arguments."""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description="Merges two or more YAML/Compatible files together.",
        epilog="""
            The CONFIG file is an INI file with up to three sections:
            [defaults] Sets equivalents of -a|--anchors, -A|--arrays,
                       -H|--hashes, and -O|--aoh.
            [rules]    Each entry is a YAML Path assigning -A|--arrays,
                       -H|--hashes, or -O|--aoh for precise nodes.
            [keys]     Wherever -O|--aoh=DEEP, each entry is treated as a
                       record with an identity key.  In order to match RHS
                       records to LHS records, a key must be known and is
                       identified on a YAML Path basis via this section.
                       Where not specified, the first attribute of the first
                       record in the Array-of-Hashes is presumed the identity
                       key for all records in the set.

            The left-to-right order of YAML_FILEs is significant.  Except
            when this behavior is deliberately altered by your options, data
            from files on the right overrides data in files to their left.  At
            least two YAML_FILEs are required.  Only one may be the -
            pseudo-file.  When only one YAML_FILE is provided, it cannot be the
            - pseudo-file and in this special-case - will be inferred as the
            second YAML_FILE as long as you are running this program without a
            TTY (unless you set --nostdin|-S).
            For more information about YAML Paths, please visit
            https://github.com/wwkimball/yamlpath."""
    )
    parser.add_argument("-V", "--version", action="version",
                        version="%(prog)s " + MY_VERSION)

    parser.add_argument(
        "-c", "--config", help=(
            "INI syle configuration file for YAML Path specified\n"
            "merge control options"))

    parser.add_argument(
        "-a", "--anchors",
        choices=[l.lower() for l in AnchorConflictResolutions.get_names()],
        type=str.lower,
        help=(
            "means by which Anchor name conflicts are resolved\n"
            "(overrides [defaults]anchors set via --config|-c and\n"
            "cannot be overridden by [rules] because Anchors apply\n"
            "to the whole file); default=stop"))
    parser.add_argument(
        "-A", "--arrays",
        choices=[l.lower() for l in ArrayMergeOpts.get_names()],
        type=str.lower,
        help=(
            "default means by which Arrays are merged together\n"
            "(overrides [defaults]arrays but is overridden on a\n"
            "YAML Path basis via --config|-c); default=all"))
    parser.add_argument(
        "-H", "--hashes",
        choices=[l.lower() for l in HashMergeOpts.get_names()],
        type=str.lower,
        help=(
            "default means by which Hashes are merged together\n"
            "(overrides [defaults]hashes but is overridden on a\n"
            "YAML Path basis in [rules] set via --config|-c);\n"
            "default=deep"))
    parser.add_argument(
        "-O", "--aoh",
        choices=[l.lower() for l in AoHMergeOpts.get_names()],
        type=str.lower,
        help=(
            "default means by which Arrays-of-Hashes are merged\n"
            "together (overrides [defaults]aoh but is overridden on\n"
            "a YAML Path basis in [rules] set via --config|-c);\n"
            "default=all"))

    parser.add_argument(
        "-m", "--mergeat",
        metavar="YAML_PATH",
        default="/",
        help=(
            "YAML Path indicating where in left YAML_FILE the right\n"
            "YAML_FILE content is to be merged; default=/"))

    parser.add_argument(
        "-o", "--output",
        help=(
            "Write the merged result to the indicated file (or\n"
            "STDOUT when unset)"))

    parser.add_argument(
        "-D", "--document-format",
        choices=[l.lower() for l in OutputDocTypes.get_names()],
        type=str.lower,
        default="auto",
        help=(
            "Force the merged result to be presented in one of the\n"
            "supported formats or let it automatically match the type\n"
            "of the first document; default=auto"))

    parser.add_argument(
        "-S", "--nostdin", action="store_true",
        help=(
            "Do not implicitly read from STDIN, even when there are\n"
            "no - pseudo-files in YAML_FILEs with a non-TTY session"))

    noise_group = parser.add_mutually_exclusive_group()
    noise_group.add_argument(
        "-d", "--debug", action="store_true",
        help="output debugging details")
    noise_group.add_argument(
        "-v", "--verbose", action="store_true",
        help="increase output verbosity")
    noise_group.add_argument(
        "-q", "--quiet", action="store_true",
        help=(
            "suppress all output except errors (implied when\n"
            "-o|--output is not set)"))

    parser.add_argument(
        "rhs_files", metavar="YAML_FILE", nargs="+",
        help=(
            "one or more YAML files to merge, order-significant;\n"
            "use - to read from STDIN"))
    return parser.parse_args()

def validateargs(args, log):
    """Validate command-line arguments."""
    has_errors = False

    # There must be at least two input files
    input_file_count = len(args.rhs_files)
    if (input_file_count == 0
        or input_file_count == 1 and (
            sys.stdin.isatty()
            or args.rhs_files[0].strip() == '-'
            or args.nostdin)
    ):
        has_errors = True
        log.error(
            "There must be at least two YAML_FILEs and only one may be the -"
            " pseudo-file, explicit or implied.")

    # There can be only one -
    pseudofile_count = 0
    for infile in args.rhs_files:
        if infile.strip() == '-':
            pseudofile_count += 1
    if pseudofile_count > 1:
        has_errors = True
        log.error("Only one YAML_FILE may be the - pseudo-file.")

    # When set, the configuration file must be a readable file
    if args.config and not (
            isfile(args.config)
            and access(args.config, R_OK)
    ):
        has_errors = True
        log.error(
            "INI style configuration file is not readable:  {}"
            .format(args.config))

    # When set, the output file must not already exist
    if args.output:
        if exists(args.output):
            has_errors = True
            log.error("Output file already exists:  {}".format(args.output))
    else:
        # When dumping the document to STDOUT, mute all non-errors
        args.quiet = True
        args.verbose = False
        args.debug = False

    if has_errors:
        sys.exit(1)

def main():
    """Main code."""
    def process_rhs(merger: Merger, rhs_yaml: Any, rhs_file: str):
        # Except for - (STDIN), each YAML_FILE must actually be a file; because
        # merge data is expected, this is a fatal failure.
        if rhs_file != "-" and not isfile(rhs_file):
            log.error("Not a file:  {}".format(rhs_file))
            return 2

        log.info("Processing {}..."
                 .format("STDIN" if rhs_file == "-" else rhs_file))

        # Try to open the file; failures are fatal
        rhs_data = get_yaml_data(rhs_yaml, log, rhs_file, allow_multidoc=True)
        if rhs_data is None:
            # An error message has already been logged
            return 3

        # Merge the new RHS into the prime LHS
        exit_state = 0
        try:
            merger.merge_with(rhs_data)
        except MergeException as mex:
            log.error(mex)
            exit_state = 4
        except YAMLPathException as yex:
            log.error(yex)
            exit_state = 5

        return exit_state

    def process_multidoc(merger: Merger, docs: GeneratorType):
        exit_state = 0
        doc_number = 0
        for doc in docs:
            doc_number += 1
            log.info("Processing multi-doc part {}".format(doc_number))

            if doc is None:
                continue

            # Merge the new RHS into the prime LHS
            log.debug("\n")
            log.debug(
                "yaml_merge::main::process_multidoc:  Attempting to merge with"
                " RHS doc:")
            log.debug(doc)
            try:
                merger.merge_with(doc)
            except MergeException as mex:
                log.error(mex)
                exit_state = 6
            except YAMLPathException as yex:
                log.error(yex)
                exit_state = 7

        return exit_state

    args = processcli()
    log = ConsolePrinter(args)
    validateargs(args, log)

    # The first input file is the prime
    fileiterator = iter(args.rhs_files)
    prime_yaml = get_yaml_editor()
    prime_file = next(fileiterator)
    consumed_stdin = prime_file.strip() == '-'
    prime_data = get_yaml_data(
        prime_yaml, log, prime_file, allow_multidoc=False)

    if isinstance(prime_data, GeneratorType):
        log.debug(
            "yaml_merge::main:  Got a multi-document source file, {}."
            .format(prime_file))
        lhs_is_multidoc = True
        lhs_data = next(prime_data, None)
    else:
        log.debug(
            "yaml_merge::main:  Got a single-document source file, {}."
            .format(prime_file))
        lhs_is_multidoc = False
        lhs_data = prime_data

    # lhs_is_multidoc = isinstance(prime_data, GeneratorType)
    # lhs_data = next(prime_data, None) if lhs_is_multidoc else prime_data
    if lhs_data is None:
        # An error message has already been logged
        log.critical(
            "The first input file, {}, has nothing to merge into."
            .format(prime_file), 1)
    merger = Merger(log, lhs_data, MergerConfig(log, args))

    # Merge additional documents from the prime data, if any
    exit_state = 0
    if lhs_is_multidoc:
        exit_state = process_multidoc(merger, prime_data)
        if exit_state != 0:
            # An error message has already been logged
            sys.exit(exit_state)

    # Merge additional input files into the prime
    rhs_yaml = get_yaml_editor()
    for rhs_file in fileiterator:
        log.debug(
            "yaml_merge::main:  Processing next file, {}".format(rhs_file))
        proc_state = process_rhs(merger, rhs_yaml, rhs_file)

        if rhs_file.strip() == '-':
            consumed_stdin = True

        if proc_state != 0:
            exit_state = proc_state
            break

    # Check for a waiting STDIN document
    if (exit_state == 0
        and not consumed_stdin
        and not args.nostdin
        and not sys.stdin.isatty()
    ):
        exit_state = process_rhs(merger, rhs_yaml, '-')

    # Output the final document
    if exit_state == 0:
        merger.prepare_for_dump(prime_yaml)
        if args.output:
            with open(args.output, 'w') as yaml_dump:
                prime_yaml.dump(merger.data, yaml_dump)
        else:
            prime_yaml.dump(merger.data, sys.stdout)

    sys.exit(exit_state)

if __name__ == "__main__":
    main()  # pragma: no cover
