"""Helper functions for Music Project"""

from jinja2 import StrictUndefined

from flask import Flask, render_template, request, flash, redirect, session
from flask_debugtoolbar import DebugToolbarExtension

from model import (User, Concert, Event, Instrument, Owner, Group, Performer,
                   PerformerGroup, Piece, SheetMusic, AudioFile, Provider,
                   GroupSheet, ConcertSheet, PerformerInstrument, Assignment,
                   EventAssignment, Genre, PieceGenre, SheetMusicProvider,
                   UserPiece, UserSheet, UserAudioFile, SheetMusicOwner,
                   connect_to_db, db)

import requests
# To get text from CPDL pages, need Beautiful Soup!!
from bs4 import BeautifulSoup
# For Beautiful Soup, need lxml's html
from lxml import html
# For Beautiful Soup, need regex
import re

####### HELPER FUNCTIONS USED IN SERVER.PY #####################################


def parse_search_results(results):
    """Converts search results page id and title data into a dictionary and
       returns its items as a list of tuples."""

    pages = results['query']['pages']

    data = {}

    for page_id, page in pages.items():
        title = page['title']
        data[page_id] = title

    return data.items()


def parse_page_results(results, pg_id):
    """Returns each page's results."""
    # Get the page title - abbreviated to pass into function.
    ttl = results['parse']['title'].split("(")[0]
    # Pull data from the page's html - first, get the page "text" from the json.
    page_txt = results['parse']['text']['*']

    # Make beautiful soup from page text's html
    soup = BeautifulSoup(page_txt, "lxml")

    # Abbreviated variables for brevity to pass into the function!
    ol = None       # original_language
    to = None       # text_original
    te = None       # text_english
    comp = 'Composer required'     # composer (not nullable!)
    lrc = None      # lyricist
    onv = None      # original_num_voices
    ov = None       # original_voicing
    # genre = None
    oi = None       # original instrumentation
    desc = None     # description
    genres = []
    # editors = []
    # ed_notes = [] # edition_notes
    # copyrights = []

    # Get general info about the piece for PIECE table & GENRE table:
    for b_tag in soup('b'):
        if "Composer:" in b_tag.contents:
            comp = b_tag.next_element.next_element.next_element.string
        if "Lyricist:" in b_tag.contents:
            lrc = b_tag.next_element.next_element.next_element.string
        if "Number of voices:" in b_tag.contents:
            onv = int((b_tag.next_element.next_element.string)
                      .replace("vv", "").replace("v", ""))
        if "Voicing:" in b_tag.contents or "Voicings:" in b_tag.contents:
            ov = b_tag.parent.get_text().split(":")[2].split("Genre")[0]
    ## GENRE IS A LIST, need to iterate & add each individually to GENRE table.
        if "Genre:" in b_tag.contents:
            cats = b_tag.parent.get_text().strip().split("Genre:")[-1]
            genres = cats.split(",")
        if "Language:" in b_tag.contents:
            ol = b_tag.next_element.next_element.next_element.string
        if "Instruments:" in b_tag.contents:
            oi = b_tag.next_element.next_element.next_element.string
        if "Description:" in b_tag.contents:
            desc = b_tag.parent.get_text().replace("Description: ", '')

    # Get the year published for PIECE table.
    pb_yr = None

    if soup.find('a', title=(re.compile("Category:\d\d\d\d works"))):
        pb_yr = soup.find('a', title=(re.compile("Category:\d\d\d\d works"))).string

    # Get the original language's text, if any provided, for PIECE table.
    if soup.big and soup.big.contents[1]:
        to = str(soup('div', 'poem', 'p')[0].p)

    # Look to see if any text in English. If so, add related 'poem' to PIECE table.
    if soup('big') and ol != 'English':
        for i, big_tag in enumerate(soup('big')):
            if 'English' in big_tag.contents[1]:
                te = str(soup('div', 'poem', 'p')[i].p)

    # Add the piece to Piece table and return the piece_id.
    piece_id = add_piece(ttl, pg_id, comp, lrc, pb_yr, onv, ov, ol, oi, to, te, desc)

    # If there were genre(s) on this page: make an empty list to hold genre id(s).
    if genres:
        genre_ids = []

        # Check whether each genre already exists - if so, add id to list. If
        # not, create it and add the id.
        for name in genres:
            if db.session.query(Genre.name).filter_by(name=name).first():
                genre_id = db.session.query(Genre.genre_id).filter_by(name=name).first()
                genre_ids.append(genre_id)
            else:
                genre_id = add_genre(name)
                genre_ids.append(genre_id)

        # Add piece & genre ids to the PieceGenre association table, if not there.
        piece_genres = db.session.query(PieceGenre.genre_id).filter_by(piece_id=piece_id).all()

        for genre_id in genre_ids:
            if genre_id not in piece_genres:
                add_piece_genre(piece_id, genre_id)

    ############ NEW - BETTER WAY TO GET CPDLs + ALL FILES ############

    # The only place <li> tags are used is for CPDL bulleted list - so, grabbing
    # all CPDL #s, file urls and edition info (for "Sheet" and "AudioFile" tables)
    # based on whether the page has <li> tags.

    # Cpdl numbers are the dictionary keys, and each edition's information will
    # be the values: each pdf, [audiofile], [filetype], [url], editor, copyright,
    # and edition notes associated with each CPDL number.
    cpdl_dict = {}

    if soup('li'):
        for i, cpdl in enumerate(soup('li')):
            if not cpdl.b:
                continue
            else:
                cpdl_dict[i] = {'cpdl': cpdl.b.font.string,
                                'files': []}
                for a in cpdl.b.parent('a'):
                    # Turns out there are both internal AND external pdf links, so
                    # checking the class and then either adding the cpdl info or not.
                    if 'internal' in a['class']:
                        cpdl_dict[i]['files'].append((a['href'].split(".")[-1],
                                                     "http://www1.cpdl.org" + a['href']))
                    else:
                        cpdl_dict[i]['files'].append((a['href'].split(".")[-1],
                                                     a['href']))

        print "\n\n\nFIRST DICT = CPDLS, URLS" + str(cpdl_dict)

        # Since we know there were CPDL #s, grabbing the other info related to
        # each edition of sheet music here, and adding to the edition_info list.
        for j, dd in enumerate(soup('dd')):
            # the text for each CPDL splits into 2 strings per CPDL #, so:
            if not j % 2:
                # Editor, Copyright('lic') @ even "i", so (i/2) gives correct dict key #
                cpdl_dict[j/2]['editor'] = dd.get_text().split(":")[1].rstrip().split(" (")[0].strip()
                cpdl_dict[j/2]['lic'] = dd.get_text().split(": ")[-1].strip()
            if j % 2:
                #  Edition notes @ odd "i"s, so ((i - 1 )/ 2) gives correct dict key #
                cpdl_dict[(j-1)/2]['ednote'] = dd.get_text().split(":")[1].rstrip().split(" (")[0].strip()

        # Use cpdl_dict to add Sheet & Files to the database. IGNORE any CPDL #
        # edition that does not have a PDF as the first file!!! (ie, directs to
        # another website, or etc - only want the editions w/a PDF of the music.)
        print "\n\n\nSECOND CPDL DICT w/ED NOTES" + str(cpdl_dict)

        for k, edition in enumerate(cpdl_dict):
            if cpdl_dict[k]['files'][0][0] == 'pdf':
                sheet_id = add_sheet(piece_id,
                                     cpdl_dict[k]['files'][0][1],
                                     cpdl_dict[k]['cpdl'],
                                     cpdl_dict[k]['editor'],
                                     cpdl_dict[k]['ednote'],
                                     cpdl_dict[k]['lic'])
                # Add the files for each sheet, if any.
                if cpdl_dict[k]['files'][1]:
                    for n, fle in enumerate(cpdl_dict[k]['files'][1:]):
                        if len(cpdl_dict[k]['files'][n + 1][0]) < 5:
                            (add_file(sheet_id,
                                      cpdl_dict[k]['files'][n + 1][0],
                                      cpdl_dict[k]['files'][n + 1][1]))
                            print "Added {}".format(cpdl_dict[k]['files'][n + 1][1])
    return piece_id

######### HELPER FUNCTIONS FOR THE ABOVE FUNCTIONS - NOT USED ELSEWHERE ########


def get_urls(filelist, num):
    """Takes names of all page's images, sorts them, sends arequest to cpdl, and
        returns each image's url, sorted to match the initial list's order."""
    # Puts the image filelist into the payload.
    payload = {
        'titles': filelist,
        'iiprop': 'url',
    }
    # Sends request to cpdl.org & captures the request's results as r4.
    r4 = requests.get(
        'http://www1.cpdl.org/wiki/api.php?action=query&format=json&prop=imageinfo',
        params=payload,
    )

    # Gets the url data from the results json and saves it as 'urls_raw'.
    urls_raw = []

    for i in range(num):
        url = r4.json()['query']['pages'].values()[i-1]['imageinfo'][0]['url']
        urls_raw.append(url)
    # Sort the urls to be in the same order as the file_tuples list
    urls = sorted(urls_raw, key=lambda x: x.split('/')[7])

    return urls


def add_piece(ttl, pg_id, comp, lrc, pb_yr, onv, ov, ol, oi, to, te, desc):
    """Adds piece to the database."""

    piece = Piece(title=ttl,
                  page_id=pg_id,
                  composer=comp,
                  lyricist=lrc,
                  publication_year=pb_yr,
                  original_num_voices=onv,
                  original_voicing=ov,
                  original_language=ol,
                  original_instrumentation=oi,
                  text_original=to,
                  text_english=te,
                  description=desc)

    # Add to the session.
    db.session.add(piece)

    # Commit the session/data to the dbase.
    db.session.commit()

    return piece.piece_id


def add_genre(name):
    """Adds genres to the database."""

    genre = Genre(name=name)

    # Add to the session.
    db.session.add(genre)

    # Commit the session/data to the dbase.
    db.session.commit()

    return genre.genre_id


def add_piece_genre(piece_id, genre_id):
    """Adds piece's genre association to the database."""

    piece_genre = PieceGenre(piece_id=piece_id,
                             genre_id=genre_id)
    # Add to the session.
    db.session.add(piece_genre)

    # Commit the session/data to the dbase.
    db.session.commit()

    return piece_genre.pg_id


def add_sheet(pid, url, cpdl, ed, ednote, lic):
    """Adds a sheet to the database."""

    sheet = SheetMusic(piece_id=pid,
                       music_url=url,
                       cpdl_num=cpdl,
                       editor=ed,
                       edition_notes=ednote,
                       license_type=lic)

    # Add to the session.
    db.session.add(sheet)

    # Commit the session/data to the dbase.
    db.session.commit()

    return sheet.sheet_id


def add_file(sheet_id, file_type, url):
    """Adds an AudioFile to the database."""

    audiofile = AudioFile(sheet_id=sheet_id,
                          file_type=file_type,
                          url=url)

    # Add to the session.
    db.session.add(audiofile)

    # Commit the session/data to the dbase.
    db.session.commit()

    return audiofile.file_id


def add_sheet_provider(sheet_id, provider_id):
    """Adds a Provider to the database."""

    sheetprovider = SheetMusicProvider(sheet_id=sheet_id,
                                       provider_id=provider_id)

    # Add to the session.
    db.session.add(sheetprovider)

    # Commit the session/data to the dbase.
    db.session.commit()

    return sheetprovider.provider_id


############ FUNCTIONS TO MANIPULATE THE DATABASE ###########################
def add_piece_to_library(user_id, piece_id):
    """Adds a UserPiece to the database, and the User's library (of pieces)."""

    userpiece = UserPiece(piece_id=piece_id,
                          user_id=user_id)

    # Add to the session.
    db.session.add(userpiece)

    # Commit the session/data to the dbase.
    db.session.commit()

    return userpiece.up_id


def add_sheet_to_library(user_id, sheet_id):
    """Adds a UserSheet to the database, and the User's library (of sheet music)."""

    usersheet = UserSheet(sheet_id=sheet_id,
                          user_id=user_id)

    # Add to the session.
    db.session.add(usersheet)

    # Commit the session/data to the dbase.
    db.session.commit()

    return usersheet.us_id


def add_audiofile_to_library(user_id, file_id):
    """Adds a UserAudioFile to the database, and the User's library (of A/V files)."""

    userfile = UserAudioFile(file_id=file_id,
                             user_id=user_id)

    # Add to the session.
    db.session.add(userfile)

    # Commit the session/data to the dbase.
    db.session.commit()

    return userfile.uaf_id


def del_piece_from_library(user_id, piece_id):
    """Deletes a UserPiece from the database, and the User's library (of pieces)."""

    userpiece = UserPiece.query.filter(UserPiece.user_id == user_id,
                                       UserPiece.piece_id == piece_id).first()

    # Delete from the session.
    db.session.delete(userpiece)

    # Commit the session/data to the dbase.
    db.session.commit()


def del_sheet_from_library(user_id, sheet_id):
    """Deletes a UserSheet from the database, and the User's library (of sheet
        music)."""

    usersheet = UserSheet.query.filter(UserSheet.user_id == user_id,
                                       UserSheet.sheet_id == sheet_id).first()

    # Delete from the session.
    db.session.delete(usersheet)

    # Commit the session/data to the dbase.
    db.session.commit()


def del_audiofile_from_library(user_id, file_id):
    """Deletes a UserAudioFile from the database, and the User's library (of
        audio files)."""

    userfile = UserAudioFile.query.filter(UserAudioFile.user_id == user_id,
                                          UserAudioFile.file_id == file_id).first()

    # Delete from the session.
    db.session.delete(userfile)

    # Commit the session/data to the dbase.
    db.session.commit()

#####################  OLD ATTEMPTS = DELETE? ##################################

    # ## Editors IS A LIST, need to iterate & add each individually to SHEET table.
    #     if "Editor:" in b_tag.contents:
    #         editor = b_tag.next_element.next_element.next_element.string
    #         editors.append(editor)
    # ## Ed_notes IS A LIST, need to iterate & add each individually to SHEET table.
    #     if "Edition notes:" in b_tag.contents:
    #         ednote = b_tag.parent.get_text().replace("Edition notes: ", '')
    #         ed_notes.append(ednote)
    # ## Copyrights IS A LIST, need to iterate & add each individually to SHEET table.
    #     if "Copyright:" in b_tag.contents:
    #         copyright = b_tag.next_element.next_element.next_element.string
    #         copyrights.append(copyright)

######### OLD WAY - GETTING SHEET & AUDIOFILE TABLES DATA #####################

    # # Get CPDL numbers for each piece's sheet music/files for SHEET and AudioFile
    # # tables.
    # # NB: CPDL # list correlates to may other lists (of items or dicts).
    # #     Use this list to pull the correct index # or key for dbase entry!
    # cpdl_nums = map(lambda x: x.string, soup('font'))

    # # Get all the image files from the page, and cut out the first 2 items, we
    # # then have all of the sheet music and audio files from the page.
    # images = results['parse']['images']
    # image_names = images[2:]

    # ###### TESTING = TRYING TO GET ALL URLs IN ONE QUERY...BETTER/FASTER than
    # # individual quesries for each file? ######

    # # Make sure there are images - if not, return 'no files' message to user.
    # if image_names != []:
    #     # Create a number to assign each file to a group, in order, that will later
    #     # correlate to the order of the CPDL #s list.
    #     pdf_2_cpdl_index = 0
    #     file_name_type_idx = [(images[pdf_2_cpdl_index], 'pdf', pdf_2_cpdl_index)]
    #     # Since all records have the pdf first, then audio files, using pdf to
    #     # split list of "images", AKA files!
    #     for image in image_names[1:]:
    #         if image.split('.')[-1] == 'pdf':
    #             pdf_2_cpdl_index += 1
    #             file_name_type_idx.append((image, 'pdf', pdf_2_cpdl_index))
    #         else:
    #             file_name_type_idx.append((image, str(image.split('.')[-1]), pdf_2_cpdl_index))

    #     # Sort tuples list so it matches the order of the urls list, below.
    #     files_sorted = sorted(file_name_type_idx)

    #     # Format the list of images to be used in an API query.
    #     imagelist = []

    #     for image in image_names:
    #         prep = "File:" + image
    #         imagelist.append(prep)
    #     # Sort the image files so the sorted results are in the same order, and
    #     # can be associated later.
    #     imagesort = sorted(imagelist)

    #     filelist = "|".join(imagesort)

    #     urls = get_urls(filelist, len(imagesort))

    #     # All pieces being parsed in this way are CPDL, provider id (prid) 1.
    #     provider_id = 1
    #     sheet_ids = []

    #     # files_sorted is a list of tuples. Each tuple contains 3 pieces of file
    #     # info:
    #     #   @ file_info[0] = filename (in same order and number as in the url list),
    #     #   @ file_info[1] = type of file,
    #     #   @ file_info[2] = cpdl list index # - also the index for related "short"
    #     #                    lists that have one piece of data per cpdl #, such as
    #     #                    editor, edition & license (copyright) info.
    #     # First, pulling all PDFs - each pdf contains the SHEET of sheet music.
    #     # Adding each Sheet to the dbase here, and collecting the sheet_ids as
    #     # a list.
    #     for i, file_info in enumerate(files_sorted):
    #         if file_info[1] == 'pdf':
    #             url = urls[i]
    #             cpdl = cpdl_nums[file_info[2]]
    #             ed = editors[file_info[2]]
    #             ednote = ed_notes[file_info[2]]
    #             lic = copyrights[file_info[2]]
    #             sheet_id = add_sheet(piece_id,
    #                                  prid,
    #                                  url,
    #                                  cpdl,
    #                                  ed,
    #                                  ednote,
    #                                  lic)
    #             sheet_ids.append((file_info[2], sheet_id))

    #     # Sort sheet ids by cpdl index number, to be sure that the non-pdf files
    #     # are each associated with the correct piece of sheet music. Each audio
    #     # file is connected to its specific Sheet using the sheet_ids list (a
    #     # sheet can have 0 or more associated files).

    #     sheet_ids_sorted = sorted(sheet_ids)

    #     file_ids = []

    #     for i, file_info in enumerate(files_sorted):
    #         if not file_info[1] == 'pdf':
    #             sheet_id = sheet_ids_sorted[file_info[2]][1]
    #             url = urls[i]
    #             file_type = file_info[1]

    #             file_id = add_file(sheet_id, file_type, url)
    #             file_ids.append(file_id)

    ################# OTHER OLD STUFF ##########################################

    # Get each piece of sheet music (.pdf) and audio files for each "user/editor"
    # and save as a dict of files by each cpdl #

    # OOPS - some pages have no PDF, links to a WEB PAGE...don't really want to
    # offer files on these, just show piece data??? Hmm...decide! For now,
    # # proceeding with what to do if there ARE pdf (sheet music) files.
    # if images != []:
    #     pieces_dict = {'pdf': get_file_url(images[0])}
    #     # Since all records have the pdf first, then audio files, using pdf to
    #     # split list of "images", AKA files!
    #     for image in images[1:]:
    #         if image.split('.')[-1] == 'pdf':
    #             pieces_dict_list.append(pieces_dict)
    #             pieces_dict = {'pdf': get_file_url(image)}
    #         else:
    #             pieces_dict[str(image.split('.')[-1])] = get_file_url(image)
    #     pieces_dict_list.append(pieces_dict)
    # else:
    #     pass # ADD HERE = maybe alert/flash that no files + user upload option?

#     # Using unique cpdl numbers as the keys, join up the above files data as the
#     # values, using zip.
#     pieces_by_cpdl = {cpdl: piece for cpdl, piece in zip(cpdl_nums,
#                                                          pieces_dict_list)}

#     # One way to get the text titles for the text/translations:
#     text_titles = map(lambda x: list(x[0].descendants)[1],
#                       filter(lambda x: x, map(lambda x: x('big'), soup('b'))))

#     # ****** Not yet returning full page results!! Need to finish getting all
#     # page data from API's json!! *******************
#     return pieces_by_cpdl
#     # data = {}

#     # for page_id, page in pages.items():
#     #     title = page['title']
#     #     data[page_id] = title

    # return urls


# def get_file_url(image):
#     """Takes an image name, sends request to cpdl, and returns the image's url."""
#     # Puts the image into the payload.
#     payload = {
#         'titles': 'File:' + image,
#         'iiprop': 'url',
#     }
#     # Sends request to cpdl.org & captures the request's results as r4.
#     r4 = requests.get(
#         'http://www1.cpdl.org/wiki/api.php?action=query&format=json&prop=imageinfo',
#         params=payload,
#     )

#     # Gets the url data from the results json and saves it as 'url'.
#     url = r4.json()['query']['pages'].values()[0]['imageinfo'][0]['url']

#     return url
