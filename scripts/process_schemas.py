"""
A script to generate the schemas for the GA4GH protocol. We download
the Avro definitions of the GA4GH protocol and use it to generate
the Python class definitions in ga4gh/_protocol_definitions.py.
"""
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import glob
import json
import shutil
import os.path
import tarfile
import tempfile
import argparse
import textwrap
import re

import avro.schema

import utils


HEADER_COMMENT = """
DO NOT EDIT THIS FILE!!
This file is automatically generated by the process_schemas.py program
in the scripts directory. It is not intended to be edited directly. If
you need to update the GA4GH protocol classes, please run the script
on the appropriate schema version.
"""


class SchemaClass(object):
    """
    Class to convert an Avro JSON definition of a GA4GH type into the
    corresponding Python class.
    """
    def __init__(self, sourceFile):
        self.sourceFile = sourceFile
        with open(sourceFile) as sf:
            self.schemaSource = sf.read()
            self.schema = avro.schema.parse(self.schemaSource)
        self.name = self.schema.name

    def getFields(self):
        """
        Returns the list of avro fields sorted in order of name.
        """
        return sorted(self.schema.fields, key=lambda f: f.name)

    def isSearchRequest(self):
        """
        Returns True if the class we are converting is a subclass of
        SearchRequest, and False otherwise.
        """
        return re.search('Search.+Request', self.name) is not None

    def isSearchResponse(self):
        """
        Returns True if the class we are converting is a subclass of
        SearchResponse, and False otherwise.
        """
        return re.search('Search.+Response', self.name) is not None

    def getValueListName(self):
        """
        Returns the name of the list used to store values in a page for
        a SearchRequest subclass.
        """
        assert self.isSearchResponse()
        names = [field.name for field in self.getFields()]
        # We assume that there are exactly two fields in every
        # SearchResponse: next_page_token and the value list.
        names.remove('next_page_token')
        assert len(names) == 1
        return names[0]

    def getEmbeddedTypes(self):
        """
        Returns the set of embedded types in this class.
        """
        # TODO need to clarify how we operate on Unions here. The current
        # code will break when we move to schema version 0.6 as we are
        # no longer assured that the first element of the union is null.
        # This would be a good opportunity to tidy this up.
        ret = []
        if isinstance(self.schema, avro.schema.RecordSchema):
            for field in self.getFields():
                if isinstance(field.type, avro.schema.ArraySchema):
                    if isinstance(field.type.items, avro.schema.RecordSchema):
                        ret.append((field.name, field.type.items.name))
                elif isinstance(field.type, avro.schema.RecordSchema):
                    ret.append((field.name, field.type.name))
                elif isinstance(field.type, avro.schema.UnionSchema):
                    t0 = field.type.schemas[0]
                    t1 = field.type.schemas[1]
                    if (isinstance(t0, avro.schema.PrimitiveSchema) and
                            t0.type == "null"):
                        if isinstance(t1, avro.schema.RecordSchema):
                            ret.append((field.name, t1.name))
                    else:
                        raise Exception("Schema union assumptions violated")
        return ret

    def formatSchema(self):
        """
        Formats the schema source so that we can print it literally
        into a Python source file.
        """
        schema = json.loads(self.schemaSource)
        stack = [schema]
        # Strip out all the docs
        while len(stack) > 0:
            elm = stack.pop()
            if "doc" in elm:
                elm["doc"] = ""
            for value in elm.values():
                if isinstance(value, dict):
                    stack.append(value)
                elif isinstance(value, list):
                    for dic in value:
                        if isinstance(dic, dict):
                            stack.append(dic)
        jsonData = json.dumps(schema)
        output = "\n".join(textwrap.wrap(jsonData)) + "\n"
        return output

    def writeRequiredFields(self, outputFile):
        """
        Writes a string encoding the set of required fields (i.e those
        fields that do not have a default value)
        """
        fields = []
        for field in self.getFields():
            if not field.has_default:
                fields.append(field)
        if len(fields) < 1:
            self._writeWithIndent('requiredFields = set([])', outputFile)
        else:
            self._writeWithIndent('requiredFields = set([', outputFile)
            for field in fields:
                string_ = '"{0}",'.format(field.name)
                self._writeWithIndent(string_, outputFile, 2)
            self._writeWithIndent('])', outputFile)

    def writeConstructor(self, outputFile):
        # Force using slots to avoid the overhead of a dict per object;
        # when a query returns hundreds of thousands of calls this can
        # save a hundred megabytes or more.
        slotString = "'" + "', '".join(
            [field.name for field in self.getFields()]) + "'"
        self._writeWithIndent("__slots__ = [", outputFile)
        self._writeWrappedWithIndent(slotString, outputFile, 2)
        self._writeWithIndent("]", outputFile)
        self._writeNewline(outputFile)
        self._writeWithIndent("def __init__(self):", outputFile)
        for field in self.getFields():
            string_ = "self.{0} = {1}".format(field.name, field.default)
            self._writeWithIndent(string_, outputFile, 2)

    def writeEmbeddedTypesClassMethods(self, outputFile):
        """
        Returns the definition for the _embeddedTypes dictionary. This is a
        temporary mechanism to provide a simple path from the current
        approach to more efficient and type-safe methods that we want
        to transition to.
        """
        def writeEmbeddedTypes():
            et = self.getEmbeddedTypes()
            if len(et) == 0:
                string = "embeddedTypes = {}"
                self._writeWithIndent(string, outputFile, 2)
            else:
                string = "embeddedTypes = {"
                self._writeWithIndent(string, outputFile, 2)
                for fn, ft in self.getEmbeddedTypes():
                    string = "'{0}': {1},".format(fn, ft)
                    self._writeWithIndent(string, outputFile, 3)
                self._writeWithIndent("}", outputFile, 2)

        self._writeWithIndent("@classmethod", outputFile)
        self._writeWithIndent("def isEmbeddedType(cls, fieldName):",
                              outputFile)
        writeEmbeddedTypes()
        self._writeWithIndent("return fieldName in embeddedTypes",
                              outputFile, 2)
        self._writeNewline(outputFile)
        self._writeWithIndent("@classmethod", outputFile)
        self._writeWithIndent("def getEmbeddedType(cls, fieldName):",
                              outputFile)
        writeEmbeddedTypes()
        self._writeNewline(outputFile)
        self._writeWithIndent("return embeddedTypes[fieldName]",
                              outputFile, 2)
        self._writeNewline(outputFile)

    def write(self, outputFile):
        """
        Writes the class definition to the specified file.
        """
        superclass = "ProtocolElement"
        if isinstance(self.schema, avro.schema.EnumSchema):
            superclass = "object"
        elif self.isSearchRequest():
            superclass = "SearchRequest"
        elif self.isSearchResponse():
            superclass = "SearchResponse"
        self._writeNewline(outputFile, 2)
        string = "class {0}({1}):".format(self.schema.name, superclass)
        print(string, file=outputFile)
        doc = self.schema.doc
        if doc is None:
            doc = "No documentation"
        self._writeWithIndent('"""', outputFile)
        self._writeWrappedWithIndent(doc, outputFile)
        self._writeWithIndent('"""', outputFile)
        if isinstance(self.schema, avro.schema.RecordSchema):
            string = '_schemaSource = """\n{0}"""'.format(
                self.formatSchema())
            self._writeWithIndent(string, outputFile)
            string = 'schema = avro.schema.parse(_schemaSource)'
            self._writeWithIndent(string, outputFile)
            self.writeRequiredFields(outputFile)
            if self.isSearchResponse():
                string = '_valueListName = "{0}"'.format(
                    self.getValueListName())
                self._writeWithIndent(string, outputFile)
            self._writeNewline(outputFile)
            self.writeEmbeddedTypesClassMethods(outputFile)
            self.writeConstructor(outputFile)
        elif isinstance(self.schema, avro.schema.EnumSchema):
            # TODO make a proper Python enum here using the Python 3.4 enum?
            for symbol in self.schema.symbols:
                string = '{0} = "{0}"'.format(symbol, symbol)
                self._writeWithIndent(string, outputFile)

    def _writeWithIndent(self, string_, outputFile, indentLevel=1):
        indent = " " * (indentLevel * 4)
        toWrite = "{}{}".format(indent, string_)
        print(toWrite, file=outputFile)

    def _writeWrappedWithIndent(self, string_, outputFile, indentLevel=1):
        indent = " " * (indentLevel * 4)
        toWrite = textwrap.fill(
            string_, initial_indent=indent, subsequent_indent=indent)
        print(toWrite, file=outputFile)

    def _writeNewline(self, outputFile, numNewlines=1):
        toWrite = "\n" * (numNewlines - 1)
        print(toWrite, file=outputFile)


class SchemaGenerator(object):
    """
    Class that generates a schema in Python code from Avro definitions.
    """
    def __init__(self, version, schemaDir, outputFile, verbosity):
        self.version = version
        self.schemaDir = schemaDir
        self.outputFile = outputFile
        self.verbosity = verbosity
        self.classes = []
        for avscFile in glob.glob(os.path.join(self.schemaDir, "*.avsc")):
            self.classes.append(SchemaClass(avscFile))
        requestClassNames = [
            cls.name for cls in self.classes if cls.isSearchRequest()]
        responseClassNames = [
            cls.name for cls in self.classes if cls.isSearchResponse()]
        self.postSignatures = []
        for request, response in zip(
                requestClassNames, responseClassNames):
            objname = re.search('Search(.+)Request', request).groups()[0]
            url = '/{0}/search'.format(objname.lower())
            tup = (url, request, response)
            self.postSignatures.append(tup)
        self.postSignatures.sort()

    def writeHeader(self, outputFile):
        """
        Writes the header information to the output file.
        """
        print('"""{0}"""'.format(HEADER_COMMENT), file=outputFile)
        print("from protocol import ProtocolElement", file=outputFile)
        print("from protocol import SearchRequest", file=outputFile)
        print("from protocol import SearchResponse", file=outputFile)
        print(file=outputFile)
        print("import avro.schema", file=outputFile)
        print(file=outputFile)
        if self.version[0].lower() == 'v' and self.version.find('.') != -1:
            versionStr = self.version[1:]  # Strip off leading 'v'
        else:
            versionStr = self.version
        print("version = '{0}'".format(versionStr), file=outputFile)

    def write(self):
        """
        Writes the generated schema classes to the output file.
        """
        with open(self.outputFile, "w") as outputFile:
            self.writeHeader(outputFile)
            # Get the classnames and sort them to get consistent ordering.
            names = [cls.name for cls in self.classes]
            classes = dict([(cls.name, cls) for cls in self.classes])
            for name in sorted(names):
                if self.verbosity > 1:
                    utils.log(name)
                cls = classes[name]
                cls.write(outputFile)

            # can't just use pprint library because
            # pep8 will complain about formatting
            outputFile.write('\npostMethods = \\\n    [(\'')
            for i, tup in enumerate(self.postSignatures):
                url, request, response = tup
                if i != 0:
                    outputFile.write('     (\'')
                outputFile.write(url)
                outputFile.write('\',\n      ')
                outputFile.write(request)
                outputFile.write(',\n      ')
                outputFile.write(response)
                outputFile.write(')')
                if i == len(self.postSignatures) - 1:
                    outputFile.write(']\n')
                else:
                    outputFile.write(',\n')


class SchemaProcessor(object):
    """
    Class to download GA4GH schema definitions from github and process
    these into Python code.
    """
    def __init__(self, args):
        self.version = args.version
        self.destinationFile = args.outputFile
        self.verbosity = args.verbose
        self.tmpDir = tempfile.mkdtemp(prefix="ga4gh_")
        self.sourceTar = os.path.join(self.tmpDir, "schemas.tar.gz")
        self.avroJarPath = args.avro_tools_jar
        # Note! The tarball does not contain the leading v
        string = "schemas-{0}".format(self.version[1:])
        self.schemaDir = os.path.join(self.tmpDir, string)
        self.avroJar = os.path.join(self.schemaDir, "avro-tools.jar")
        self.sourceDir = args.inputSchemasDirectory
        self.avroPath = "src/main/resources/avro"

    def cleanup(self):
        if self.verbosity > 1:
            utils.log("Cleaning up tmp dir {}".format(self.tmpDir))
        shutil.rmtree(self.tmpDir)

    def download(self, url, destination):
        """
        Downloads the specified url and saves the result to the specified
        file.
        """
        fileDownloader = utils.FileDownloader(url, destination)
        fileDownloader.download()

    def convertAvro(self, avdlFile):
        """
        Converts the specified avdl file using the java tools.
        """
        args = ["java", "-jar", self.avroJar, "idl2schemata", avdlFile]
        if self.verbosity > 0:
            utils.log("converting {}".format(avdlFile))
        if self.verbosity > 1:
            utils.log("running: {}".format(" ".join(args)))
        if self.verbosity > 1:
            utils.runCommandSplits(args)
        else:
            utils.runCommandSplits(args, silent=True)

    def setupAvroJar(self):
        if self.avroJarPath is not None:
            self.avroJar = os.path.abspath(self.avroJarPath)
        else:
            url = "http://www.carfab.com/apachesoftware/avro/stable/java/"\
                "avro-tools-1.7.7.jar"
            self.download(url, self.avroJar)

    def getSchemaFromGitHub(self):
        """
        Downloads a tagged version of the schemas
        from the official GitHub repo.
        """
        url = "https://github.com/ga4gh/schemas/archive/{0}.tar.gz".format(
            self.version)
        self.download(url, self.sourceTar)
        with tarfile.open(self.sourceTar, "r") as tarball:
            tarball.extractall(self.tmpDir)

    def getSchemaFromLocal(self):
        """
        Copies schemas from a local directory.
        """
        destDir = os.path.join(self.schemaDir, self.avroPath)
        if not os.path.exists(destDir):
            os.makedirs(destDir)
        avdlFiles = glob.iglob(os.path.join(self.sourceDir, "*.avdl"))
        for avdlFile in avdlFiles:
            if os.path.isfile(avdlFile):
                shutil.copy2(avdlFile, destDir)

    def run(self):
        if self.sourceDir is None:
            self.getSchemaFromGitHub()
        else:
            self.getSchemaFromLocal()
        directory = os.path.join(self.schemaDir, self.avroPath)
        self.setupAvroJar()
        cwd = os.getcwd()
        os.chdir(directory)
        for avdlFile in glob.glob("*.avdl"):
            self.convertAvro(avdlFile)
        os.chdir(cwd)
        if self.verbosity > 0:
            utils.log("Writing schemas to {}".format(self.destinationFile))
        sg = SchemaGenerator(
            self.version, directory, self.destinationFile, self.verbosity)
        sg.write()


def main():
    parser = argparse.ArgumentParser(
        description="Script to process GA4GH Avro schemas, "
        "Requires java external command to run. "
        "By default, the version string is used to download the "
        "corresponding tagged version of the Avro schemas from the "
        "official ga4gh/schemas repository on GitHub. "
        "If however the -i argument is provided, locally stored .avdl "
        "(Avro definition) files in the specified directory are used "
        "instead.")
    parser.add_argument(
        "--outputFile", "-o", default="ga4gh/_protocol_definitions.py",
        help="The file to output the protocol definitions to.")
    parser.add_argument(
        "version",
        help="The tagged git release to process, e.g., v0.5.1. "
        "Taken literally if --inputSchemasDirectory is specified.")
    parser.add_argument(
        "--avro-tools-jar", "-j",
        help="The path to a local avro-tools.jar", default=None)
    parser.add_argument(
        "--inputSchemasDirectory", "-i",
        help="Path to local directory containing .avdl schema files.",
        default=None)
    # TODO is this the right approach? Maybe we should be noisy be
    # default and add in an option to be quiet.
    parser.add_argument('--verbose', '-v', action='count', default=0)
    # We don't support Python 3 right now because the Avro API is
    # different between the different versions.
    if sys.version_info >= (3, 0):
        utils.log("We don't currently support Python 3, sorry...")
        sys.exit(1)
    args = parser.parse_args()
    sp = SchemaProcessor(args)
    try:
        sp.run()
    finally:
        sp.cleanup()


if __name__ == "__main__":
    main()
