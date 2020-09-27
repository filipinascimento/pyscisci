import os
import sys
import json
import gzip

import pandas as pd
import numpy as np
from nameparser import HumanName
import requests
from lxml import etree
from io import BytesIO

# determine if we are loading from a jupyter notebook (to make pretty progress bars)
if 'ipykernel' in sys.modules:
    from tqdm.notebook import tqdm
else:
    from tqdm import tqdm

from pyscisci.datasource.readwrite import load_preprocessed_data, load_int, load_float, load_html_str, load_xml_text
from pyscisci.database import BibDataBase


class WOS(BibDataBase):
    """
    Base class for Web of Science interface.

    """

    def __init__(self, path2database='', keep_in_memory=False, show_progress=True):

        self.path2database = path2database
        self.keep_in_memory = keep_in_memory
        self.show_progress = show_progress

        self._affiliation_df = None
        self._pub_df = None
        self._journal_df = None
        self._author_df = None
        self._pub2year = None
        self._pub2ref_df = None
        self._pub2refnoself_df = None
        self._author2pub_df = None
        self._paa_df = None
        self._pub2field_df=None
        self._fieldinfo_df = None

        self.PublicationIdType = int
        self.AffiliationIdType = str
        self.AuthorIdType = str

    def download_from_source(self):

        raise NotImplementedError("The Web of Science (WOS) is proprietary owned by Clarivate Analytics.  Contact their sales team to aquire access to the data.")

    def parse_affiliations(self, preprocess = False, show_progress=False):
        raise NotImplementedError("WOS is stored as a xml archive.  Run preprocess to parse the archive.")

    def parse_authors(self, preprocess = False, process_name = True, num_file_lines = 5*10**6, show_progress=False):
        raise NotImplementedError("WOS does not contain disambiguated author information.")

    def _blank_wos_publication(self, PublicationId = 0):
        record = {}
        record['PublicationId'] = PublicationId
        record['Title'] = ''
        record['Year'] = 0
        record['Volume'] = 0
        record['Issue'] = ''
        record['Pages'] = ''
        record['JournalId'] = ''
        record['TeamSize'] = 0
        record['Date'] = 1
        record['DocType'] = ''
        record['ISSN'] = ''
        record['DOI'] = ''

        return record

    def _blank_wos_author(self, AuthorId = None):
        record = {}
        record['AuthorId'] = AuthorId
        record['FullName'] = ''
        record['FirstName'] = ''
        record['LastName'] = ''
        record['OrigAuthorName'] = ''
        return record

    def _blank_wos_affiliation(self):
        record = {}
        record['FullAddress'] = ''
        record['Organizations'] = ''
        record['SubOrganizations'] = ''
        record['City'] = ''
        record['Country'] = ''
        return record

    def _save_dataframes(self, ifile, publication_df, author_df, author_columns, paa_df):

        publication_df = pd.DataFrame(publication_df)
        publication_df['PublicationId'] = publication_df['PublicationId']
        publication_df['Year'] = publication_df['Year'].astype(int)
        publication_df['Volume'] = pd.to_numeric(publication_df['Volume'])
        publication_df['TeamSize'] = publication_df['TeamSize'].astype(int)
        publication_df.to_hdf( os.path.join(self.path2database,'publication', 'publication{}.hdf'.format(ifile)), key = 'pub', mode='w')


        author_df = pd.DataFrame(author_df, columns = author_columns)
        author_df['AuthorId'] = author_df['AuthorId'].astype(int)
        author_df.to_hdf( os.path.join(self.path2database,'author', 'author{}.hdf'.format(ifile)), key = 'author', mode='w')

        paa_df = pd.DataFrame(paa_df, columns = ['PublicationId', 'AuthorId', 'AuthorSequence', 'OrigAuthorName'])
        paa_df.to_hdf( os.path.join(self.path2database,'publicationauthoraffiliation', 'publicationauthoraffiliation{}.hdf'.format(ifile)), key = 'pa', mode='w')

    def preprocess(self, xml_directory = 'RawXML', name_space = 'http://scientific.thomsonreuters.com/schema/wok5.4/public/FullRecord', 
        process_name=True, num_file_lines=10**6, show_progress=True):
        """
        Bulk preprocess of the DBLP raw data.

        Parameters
        ----------
        :param process_name: bool, default True
            If True, then when processing the raw file, the package `NameParser <https://nameparser.readthedocs.io/en/latest/>`_
            will be used to split author FullNames.

        :param xml_file_name: str, default 'dblp.xml.gz'
            The xml file name.

        :param num_file_lines: int, default 10**6
            The processed data will be saved into smaller DataFrames, each with `num_file_lines` rows.

        :param show_progress: bool, default True
            Show progress with processing of the data.

        """

        ACCEPT_DOCTYPES = set(['article', 'inproceedings', 'proceedings', 'book', 'incollection', 'phdthesis', 'mastersthesis'])
        REJECT_DOCTYPES = set(['www'])
        DATA_ITEMS = ['title', 'booktitle', 'year', 'journal', 'ee',' url', 'month', 'mdate', 'isbn', 'publisher']
        SKIP_FIELDS = ['note', 'cite', 'cdrom', 'crossref', 'editor',  'series', 'tt', 'school', 'chapter', 'address']

        html_format_keys = ['<sub>', '</sub>', '<sup>', '</sup>', '<i>', '</i>']

        if show_progress:
            print("Starting to preprocess the WOS database.")


        for hier_dir_type in ['publication', 'author', 'publicationauthoraffiliation', 'pub2field', 'pub2ref']:

            if not os.path.exists(os.path.join(self.path2database, hier_dir_type)):
                os.mkdir(os.path.join(self.path2database, hier_dir_type))

        
        

        aname2aid = {}
        pub2year = {}


        ns = {"ns": name_space}
        xmlfiles = sorted([fname for fname in os.listdir(os.path.join(self.path2database, xml_directory)) if '.xml' in fname])

        for xml_file_name in tqdm(xmlfiles, desc='WOS xml files', leave=True, disable=not show_progress):
            
            publication_df = []
            author_df = []
            paa_df = []
            pub2field_df = []
            pub2ref_df = []

            if '.gz' in xml_file_name:
                with gzip.open(os.path.join(self.path2database, xml_directory, xml_file_name), 'r') as infile:
                    xml_file = infile.read()

            # extract the desired fields from the XML tree  #
            bytesxml = BytesIO(xml_file)
            xmltree = etree.iterparse(bytesxml, events=('end',), tag="{{{0}}}REC".format(name_space))

            if show_progress:
                print("{} Xml tree parsed, iterating through elements.".format(xml_file_name))

            last_position = 0

            for event, elem in xmltree:
                
                PublicationId = load_html_str(elem.xpath('./ns:UID', namespaces=ns)[0].text.replace('WOS:', ''))
                
                pub_record = self._blank_wos_publication(PublicationId)

                pub_record['Title'] = load_html_str(load_xml_text(elem.xpath('./ns:static_data/ns:summary/ns:titles/ns:title[@type="item"]', namespaces=ns)))
                pub_record['JournalId'] = load_html_str(load_xml_text(elem.xpath('./ns:static_data/ns:summary/ns:titles/ns:title[@type="source"]', namespaces=ns)))
                
                pub_info = elem.xpath('./ns:static_data/ns:summary/ns:pub_info', namespaces=ns)[0]
                pub_record['Year'] = load_int(pub_info.get('pubyear', ''))
                pub_record['Date'] = load_html_str(pub_info.get('sortdate', ''))
                pub_record['Volume'] = load_int(pub_info.get('vol', ''))
                pub_record['Issue'] = load_int(pub_info.get('issue', ''))

                pub2year[PublicationId] = pub_record['Year']

                pub_record['Pages'] = load_html_str(load_xml_text(elem.xpath('./ns:static_data/ns:summary/ns:pub_info/ns:page', namespaces=ns), default=''))
                
                for ident in ['ISSN', 'DOI']:
                    identobject = elem.xpath('./ns:dynamic_data/ns:cluster_related/ns:identifiers/ns:identifier[@type="{}"]'.format(ident.lower()), namespaces=ns)
                    if len(identobject) > 0:
                        pub_record[ident] =load_html_str( identobject[0].get('value', ''))
                    
                
                #load_html_str(load_xml_text(elem.xpath('./ns:dynamic_data/ns:cluster_related/ns:identifiers/ns:identifier[@type="doi"]', namespaces=ns)))

                pub_record['DocType'] = load_html_str(load_xml_text(elem.xpath('./ns:static_data/ns:summary/ns:doctypes/ns:doctype', namespaces=ns)))

                
                pub_authors = {}
                author_objects = elem.xpath('./ns:static_data/ns:summary/ns:names/ns:name[@role="author"]', namespaces=ns)
                pub_record['TeamSize'] = len(author_objects)

                for author_obj in author_objects:
                    author_record = self._blank_wos_author(None)
                    author_record['AuthorId'] = author_obj.get('dais_id', None)

                    author_record['FullName'] = load_html_str(load_xml_text(author_obj.xpath('./ns:full_name', namespaces=ns)))
                    author_record['FirstName'] = load_html_str(load_xml_text(author_obj.xpath('./ns:first_name', namespaces=ns)))
                    author_record['LastName'] = load_html_str(load_xml_text(author_obj.xpath('./ns:last_name', namespaces=ns)))

                    author_record['AffiliationOrder'] = author_obj.get('addr_no', '')
                    author_record['AffiliationOrder'] = [int(single_addr_no) for single_addr_no in author_record['AffiliationOrder'].split(' ') if len(single_addr_no) > 0]
                    
                    author_record['AuthorOrder'] = int(author_obj.get('seq_no', None))

                    pub_authors[author_record['AuthorOrder']] = author_record
                    
                    
                #contributor_objects = elem.xpath('./ns:static_data/ns:contributors/ns:contributor/ns:name[@role="researcher_id"]', namespaces=ns)

                address_objects = elem.xpath('./ns:static_data/ns:fullrecord_metadata/ns:addresses/ns:address_name/ns:address_spec', namespaces=ns)
                for addr_obj in address_objects:
                    addr_record = self._blank_wos_affiliation()

                    organization_objects = addr_obj.xpath('./ns:organizations/ns:organization[@pref="Y"]', namespaces=ns)

                    address_no = int(addr_obj.get('addr_no'))
                    article['addresses'][address_no] = address_info


                reference_objects = elem.xpath('./ns:static_data/ns:fullrecord_metadata/ns:references/ns:reference', namespaces=ns)
                for ref_obj in reference_objects:
                    for ref_elem in ref_obj:
                        if ref_elem.tag == "{{{0}}}uid".format(name_space):
                            refid = load_html_str(ref_elem.text.replace('WOS:', ''))
                            pub2ref_df.append([PublicationId, refid])
                        elif ref_elem.tag == "{{{0}}}year".format(name_space):
                            pub2year[refid] = load_int(ref_elem.text)

                print(pub_record)
                print(pub2ref_df)
                print(pub2year)
                asdgasdg

                
    """
                elif elemtag == 'name' and elem.get('role', '') == 'author':

                    AuthorCount += 1
                    fullname = load_html_str(elem.text)
                    
                elif elemtag == 'REC':
                    print(pub_record)
                    pub_record['TeamSize'] = AuthorCount

                    # reset everything
                    publication_df.append(pub_record)
                    author2pub_df.extend(pub_authors)
                    PublicationId += 1
                    pub_record = self._blank_dblp_publication(PublicationId)
                    AuthorCount = 0
                    pub_authors = []

                #pub_record['TeamSize'] = load_html_int(elem.get('count'))


                last_position += 1
                if last_position==100:
                    print(pub_record)
                    asdjlgaklsgjoia """
        
