"""
Returns zero or more YAML Paths indicating where in given YAML/Compatible data
a search expression matches.  Values and/or keys can be searched.  EYAML can be
employed to search encrypted values.

Copyright 2019 William W. Kimball, Jr. MBA MSIS
"""
import argparse
from os import access, R_OK
from os.path import isfile
from typing import Any, Generator

from ruamel.yaml.parser import ParserError
from ruamel.yaml.composer import ComposerError
from ruamel.yaml.scanner import ScannerError
from ruamel.yaml.comments import CommentedSeq, CommentedMap

from yamlpath.func import get_yaml_editor, search_matches, ensure_escaped
from yamlpath.enums import PathSeperators
from yamlpath.path import SearchTerms
from yamlpath import YAMLPath
from yamlpath.wrappers import ConsolePrinter
from yamlpath.eyaml import EYAMLProcessor

# Implied Constants
MY_VERSION = "0.0.1"

def processcli():
    """Process command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Returns zero or more YAML Paths indicating where in given\
            YAML/Compatible data a search expression matches.  Values and/or\
            keys can be searched.  EYAML can be employed to search encrypted\
            values.",
        epilog="For more information about YAML Paths, please visit\
            https://github.com/wwkimball/yamlpath."
    )
    parser.add_argument("-V", "--version", action="version",
                        version="%(prog)s " + MY_VERSION)

    required_group = parser.add_argument_group("required settings")
    required_group.add_argument(
        "-s", "--search",
        required=True,
        metavar="EXPRESSION", action="append",
        help="the search expression; can be set more than once")

    noise_group = parser.add_mutually_exclusive_group()
    noise_group.add_argument(
        "-d", "--debug",
        action="store_true",
        help="output debugging details")
    noise_group.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="increase output verbosity")
    noise_group.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="suppress all output except errors")

    parser.add_argument(
        "-t", "--pathsep",
        default="dot",
        choices=PathSeperators,
        metavar=PathSeperators.get_choices(),
        type=PathSeperators.from_str,
        help="indicate which YAML Path seperator to use when rendering\
              results; default=dot")

    keyname_group_ex = parser.add_argument_group("Key name searching options")
    keyname_group = keyname_group_ex.add_mutually_exclusive_group()
    keyname_group.add_argument(
        "-i", "--ignorekeynames",
        action="store_true",
        help="(default) do not search key names")
    keyname_group.add_argument(
        "-k", "--keynames",
        action="store_true",
        help="search key names in addition to values and array elements")
    keyname_group.add_argument(
        "-o", "--onlykeynames",
        action="store_true",
        help="only search key names (ignore all values and array elements)")

    anchor_group_ex = parser.add_argument_group(
        "Anchor handling options")
    anchor_group = anchor_group_ex.add_mutually_exclusive_group()
    anchor_group.add_argument(
        "-c", "--onlyanchors",
        action="store_true",
        help="(default) include only the original anchor in matching results")
    anchor_group.add_argument(
        "-a", "--aliases",
        action="store_true",
        help="include anchor and duplicate aliases in results")

    eyaml_group = parser.add_argument_group(
        "EYAML options", "Left unset, the EYAML keys will default to your\
         system or user defaults.  Both keys must be set either here or in\
         your system or user EYAML configuration file when using EYAML.")
    eyaml_group.add_argument(
        "-x", "--eyaml",
        default="eyaml",
        help="the eyaml binary to use when it isn't on the PATH")
    eyaml_group.add_argument("-r", "--privatekey", help="EYAML private key")
    eyaml_group.add_argument("-u", "--publickey", help="EYAML public key")

    parser.add_argument("yaml_files", metavar="YAML_FILE", nargs="+",
                        help="one or more YAML files to search")

    return parser.parse_args()

def validateargs(args, log):
    """Validate command-line arguments."""
    has_errors = False

    # Enforce sanity
    # * When set, --privatekey must be a readable file
    if args.privatekey and not (
            isfile(args.privatekey) and access(args.privatekey, R_OK)
    ):
        has_errors = True
        log.error(
            "EYAML private key is not a readable file:  " + args.privatekey
        )

    # * When set, --publickey must be a readable file
    if args.publickey and not (
            isfile(args.publickey) and access(args.publickey, R_OK)
    ):
        has_errors = True
        log.error(
            "EYAML public key is not a readable file:  " + args.publickey
        )

    # * When either --publickey or --privatekey are set, the other must also
    #   be.  This is because the `eyaml` command requires them both when
    #   decrypting values.
    if (
            (args.publickey and not args.privatekey)
            or (args.privatekey and not args.publickey)
    ):
        has_errors = True
        log.error("Both private and public EYAML keys must be set.")

    if has_errors:
        exit(1)

def search_for_paths(data: Any, terms: SearchTerms,
                     pathsep: PathSeperators = PathSeperators.DOT,
                     build_path: str = "",
                     **kwargs: bool) -> Generator[YAMLPath, None, None]:
    """
    Recursively searches a data structure for nodes matching a search
    expression.  The nodes can be keys, values, and/or elements.  When dealing
    with anchors and their aliases, the caller indicates whether to include
    only the original anchor or the anchor and all of its (duplicate) aliases.
    """
    search_values: bool = kwargs.pop("search_values", True)
    search_keys: bool = kwargs.pop("search_keys", False)
    include_aliases: bool = kwargs.pop("include_aliases", False)
    strsep = str(pathsep)
    invert = terms.inverted
    method = terms.method
    term = terms.term
    seen_anchors = []

    if isinstance(data, CommentedSeq):
        # Build the path
        if not build_path and pathsep is PathSeperators.FSLASH:
            build_path = strsep
        build_path += "["

        for idx, ele in enumerate(data):
            # Screen out aliases if the anchor has already been seen, unless
            # the caller has asked for all the duplicate results.
            if hasattr(ele, "anchor") and ele.anchor.value is not None:
                # Dealing with an anchor/alias, so ref this node by name unless
                # it is to be excluded from the search results.
                anchor_name = ele.anchor.value
                if anchor_name in seen_anchors:
                    if not include_aliases:
                        # Ignore duplicate aliases
                        continue
                else:
                    # Record only original anchor names
                    seen_anchors.append(anchor_name)

                tmp_path = "{}&{}]".format(
                    build_path,
                    ensure_escaped(anchor_name, strsep),
                )
            else:
                # Not an anchor/alias, so ref this node by its index
                tmp_path = build_path + str(idx) + "]"

            if isinstance(ele, (CommentedSeq, CommentedMap)):
                # When an element is a list-of-lists/dicts, recurse into it.
                for subpath in search_for_paths(
                        ele, terms, pathsep, tmp_path,
                        search_values=search_values, search_keys=search_keys,
                        include_aliases=include_aliases
                ):
                    yield subpath
            elif search_values:
                # Otherwise, check the element for a match unless the caller
                # isn't interested in value searching.
                matches = search_matches(method, term, ele)
                if (matches and not invert) or (invert and not matches):
                    yield YAMLPath(tmp_path)

    elif isinstance(data, CommentedMap):
        if build_path:
            build_path += strsep
        elif pathsep is PathSeperators.FSLASH:
            build_path = strsep

        pool = data.non_merged_items()
        if include_aliases:
            pool = data.items()

        for key, val in pool:
            # The key may be an anchor/alias.  The value may also be an
            # anchor/alias.  Only duplicate values shall be screened out.
            # Duplicate aliased keys are always included in the search results
            # lest there be no means of identifying their children nodes.
            # Duplicate aliased values are included in the search results only
            # when the caller has asked for them to be.
            tmp_path = build_path + ensure_escaped(
                ensure_escaped(key, "\\"), strsep)
            key_matched = False

            # Search the key when the caller wishes it.
            if search_keys:
                matches = search_matches(method, term, key)
                if (matches and not invert) or (invert and not matches):
                    key_matched = True
                    yield YAMLPath(tmp_path)

            if isinstance(val, (CommentedSeq, CommentedMap)):
                # When the value is a list/dict, recurse into it.
                for subpath in search_for_paths(
                        val, terms, pathsep, tmp_path,
                        search_values=search_values, search_keys=search_keys,
                        include_aliases=include_aliases
                ):
                    yield subpath
            elif search_values and not key_matched:
                # Otherwise, search the value when the caller wishes it, but
                # not if the key has already matched (lest a duplicate result
                # be generated).  Exclude duplicate alias values unless the
                # caller wishes to receive them.
                if hasattr(val, "anchor") and val.anchor.value is not None:
                    anchor_name = val.anchor.value
                    if anchor_name in seen_anchors:
                        if not include_aliases:
                            # Ignore duplicate aliases
                            continue
                    else:
                        # Record only original anchor names
                        seen_anchors.append(anchor_name)

                matches = search_matches(method, term, val)
                if (matches and not invert) or (invert and not matches):
                    yield YAMLPath(tmp_path)

def main():
    """Main code."""
    # Process any command-line arguments
    args = processcli()
    log = ConsolePrinter(args)
    validateargs(args, log)

    # Prepare the YAML processor
    yaml = get_yaml_editor()
    processor = EYAMLProcessor(
        log, None, binary=args.eyaml,
        publickey=args.publickey, privatekey=args.privatekey)

    # Process the input file(s)
    in_file_count = len(args.yaml_files)
    in_expressions = len(args.search)
    exit_state = 0
    for yaml_file in args.yaml_files:
        # Try to open the file
        try:
            with open(yaml_file, 'r') as fhnd:
                yaml_data = yaml.load(fhnd)
        except ParserError as ex:
            log.error("YAML parsing error {}:  {}"
                      .format(str(ex.problem_mark).lstrip(), ex.problem))
            exit_state = 3
            continue
        except ComposerError as ex:
            log.error("YAML composition error {}:  {}"
                      .format(str(ex.problem_mark).lstrip(), ex.problem))
            exit_state = 3
            continue
        except ScannerError as ex:
            log.error("YAML syntax error {}:  {}"
                      .format(str(ex.problem_mark).lstrip(), ex.problem))
            exit_state = 3
            continue

        # Process all searches
        processor.data = yaml_data
        for expression in args.search:
            expath = YAMLPath("[*{}]".format(expression))
            for result in search_for_paths(yaml_data, expath.escaped[0][1],
                                           args.pathsep):
                if in_file_count > 1:
                    if in_expressions > 1:
                        print("{}[{}]: {}".format(
                            yaml_file, expression, result))
                    else:
                        print("{}: {}".format(yaml_file, result))
                else:
                    if in_expressions > 1:
                        print("[{}]: {}".format(expression, result))
                    else:
                        print("{}".format(result))

    exit(exit_state)

if __name__ == "__main__":
    main()
