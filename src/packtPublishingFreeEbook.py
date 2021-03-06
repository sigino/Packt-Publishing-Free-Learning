import click
import datetime as dt
from itertools import chain
import json
import logging
from math import ceil
import os
import re
import sys
import configparser

from bs4 import BeautifulSoup
import requests
from requests.exceptions import ConnectionError
from slugify import slugify

from utils.anticaptcha import Anticaptcha
from utils.logger import get_logger

logger = get_logger(__name__)
logging.getLogger("requests").setLevel(logging.WARNING)  # downgrading logging level for requests

DATE_FORMAT = "%Y/%m/%d"

SUCCESS_EMAIL_SUBJECT = "{} New free Packt ebook: \"{}\""
SUCCESS_EMAIL_BODY = "A new free Packt ebook \"{}\" was successfully grabbed. Enjoy!"
FAILURE_EMAIL_SUBJECT = "{} Grabbing a new free Packt ebook failed"
FAILURE_EMAIL_BODY = "Today's free Packt ebook grabbing has failed with exception: {}!\n\nCheck this out!"

USER_AGENT_HEADER = {
    'User-Agent':
    'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/51.0.2704.103 Safari/537.36'
}
PACKT_FREE_LEARNING_URL = 'https://www.packtpub.com/packt/offers/free-learning/'

DEFAULT_PAGINATION_SIZE = 25
PACKT_API_LOGIN_URL = 'https://services.packtpub.com/auth-v1/users/tokens'
PACKT_API_PRODUCTS_URL = 'https://services.packtpub.com/entitlements-v1/users/me/products'
PACKT_PRODUCT_SUMMARY_URL = 'https://static.packt-cdn.com/products/{product_id}/summary'
PACKT_API_PRODUCT_FILE_TYPES_URL = 'https://services.packtpub.com/products-v1/products/{product_id}/types'
PACKT_API_PRODUCT_FILE_DOWNLOAD_URL =\
    'https://services.packtpub.com/products-v1/products/{product_id}/files/{file_type}'
PACKT_API_FREE_LEARNING_OFFERS_URL = 'https://services.packtpub.com/free-learning-v1/offers'
PACKT_API_USER_URL = 'https://services.packtpub.com/users-v1/users/me'
PACKT_API_FREE_LEARNING_CLAIM_URL = 'https://services.packtpub.com/free-learning-v1/users/{user_id}/claims/{offer_id}'


def slugify_book_title(title):
    """Return book title with spaces replaced by underscore and unicodes replaced by characters valid in filenames."""
    return slugify(title, separator='_', lowercase=False)


class PacktConnectionError(ConnectionError):
    """Error raised whenever fetching data from Packt page fails."""
    pass


class ConfigurationModel(object):
    """Contains all needed urls, passwords and packtpub account data stored in .cfg file"""

    def __init__(self, cfg_file_path):
        self.cfg_file_path = cfg_file_path
        self.cfg_folder_path = os.path.dirname(cfg_file_path)
        self.configuration = configparser.ConfigParser()
        if not self.configuration.read(self.cfg_file_path):
            raise configparser.Error('{} file not found'.format(self.cfg_file_path))
        self.book_infodata_log_file = self._get_config_ebook_extrainfo_log_filename()
        self.anticaptcha_clientkey = self.configuration.get("ANTICAPTCHA_DATA", 'key')
        self.my_packt_email, self.my_packt_password = self._get_config_login_data()
        self.download_folder_path, self.download_formats = self._get_config_download_data()
        if not os.path.exists(self.download_folder_path):
            message = "Download folder path: '{}' doesn't exist".format(self.download_folder_path)
            logger.error(message)
            raise ValueError(message)

    def _get_config_ebook_extrainfo_log_filename(self):
        """Gets the filename of the ebook metadata log file."""
        return self.configuration.get("DOWNLOAD_DATA", 'ebook_extra_info_log_file_path')

    def _get_config_login_data(self):
        """Gets user login credentials."""
        email = self.configuration.get("LOGIN_DATA", 'email')
        password = self.configuration.get("LOGIN_DATA", 'password')
        return email, password

    def _get_config_download_data(self):
        """Downloads ebook data from the user account."""
        download_path = self.configuration.get("DOWNLOAD_DATA", 'download_folder_path')
        download_formats = tuple(form.replace(' ', '') for form in
                                 self.configuration.get("DOWNLOAD_DATA", 'download_formats').split(','))
        return download_path, download_formats


class PacktPublishingFreeEbook(object):
    """Contains some methods to claim, download or send a free daily ebook"""

    download_formats = ('pdf', 'mobi', 'epub', 'code')
    jwt = None

    def __init__(self, cfg):
        self.cfg = cfg
        self.book_title = ""

    def login_required(func, *args, **kwargs):
        def login_decorated(self, *args, **kwargs):
            if self.jwt is None:
                self.fetch_jwt()
            return func(self, *args, **kwargs)
        return login_decorated

    def fetch_jwt(self):
        """Fetch user's JWT to be used when making Packt API requests."""
        response = requests.post(PACKT_API_LOGIN_URL, data=json.dumps({
            'username': self.cfg.my_packt_email,
            'password': self.cfg.my_packt_password,
        }))
        try:
            self.jwt = response.json().get('data').get('access')
            logger.info('JWT token has been fetched successfully!')
        except Exception:
            logger.error('Fetching JWT token failed!')

    def get_all_books_data(self):
        """Fetch all user's ebooks data."""
        logger.info("Getting your books data...")
        try:
            response = requests.get(PACKT_API_PRODUCTS_URL, headers={'authorization': 'Bearer {}'.format(self.jwt)})
            pages_total = int(ceil(response.json().get('count') / DEFAULT_PAGINATION_SIZE))
            my_books_data = list(chain(*map(self.get_single_page_books_data, range(pages_total))))
            logger.info('Books data has been successfully fetched.')
            return my_books_data
        except (AttributeError, TypeError):
            logger.error('Couldn\'t fetch user\'s books data.')

    def get_single_page_books_data(self, page):
        """Fetch ebooks data from single products API pagination page."""
        try:
            response = requests.get(
                PACKT_API_PRODUCTS_URL,
                params={
                    'sort': 'createdAt:DESC',
                    'offset': DEFAULT_PAGINATION_SIZE * page,
                    'limit': DEFAULT_PAGINATION_SIZE
                },
                headers={'authorization': 'Bearer {}'.format(self.jwt)}
            )
            return [{'id': t['productId'], 'title': t['productName']} for t in response.json().get('data')]
        except Exception:
            logger.error('Couldn\'t fetch page {} of user\'s books data.'.format(page))

    def solve_packt_recapcha(self):
        """Open Packt Free Learning website and solve ReCAPTCHA from this site."""
        response = requests.get(PACKT_FREE_LEARNING_URL, headers=USER_AGENT_HEADER)
        html = BeautifulSoup(response.text, 'html.parser')

        key_pattern = re.compile('Packt.offers.onLoadRecaptcha\(\'(.+?)\'\)')
        website_key = key_pattern.search(html.find(text=key_pattern)).group(1)

        logger.info('Started solving ReCAPTCHA on Packt Free Learning website...')
        anticaptcha = Anticaptcha(self.cfg.anticaptcha_clientkey)
        return anticaptcha.solve_recaptcha(PACKT_FREE_LEARNING_URL, website_key)

    @login_required
    def grab_ebook(self):
        """Grab Packt Free Learning ebook."""
        logger.info("Start grabbing ebook...")

        utc_today = dt.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        offer_response = requests.get(
            PACKT_API_FREE_LEARNING_OFFERS_URL,
            params={
                'dateFrom': utc_today.isoformat(),
                'dateTo': (utc_today + dt.timedelta(days=1)).isoformat()
            },
            headers={'authorization': 'Bearer {}'.format(self.jwt)}
        )
        [offer_data] = offer_response.json().get('data')
        offer_id = offer_data.get('id')
        product_id = offer_data.get('productId')

        user_response = requests.get(PACKT_API_USER_URL, headers={'authorization': 'Bearer {}'.format(self.jwt)})
        [user_data] = user_response.json().get('data')
        user_id = user_data.get('id')

        claim_response = requests.put(
            PACKT_API_FREE_LEARNING_CLAIM_URL.format(user_id=user_id, offer_id=offer_id),
            data=json.dumps({'recaptcha': self.solve_packt_recapcha()}),
            headers={'authorization': 'Bearer {}'.format(self.jwt)}
        )

        product_response = requests.get(
            PACKT_PRODUCT_SUMMARY_URL.format(product_id=product_id),
            headers={'authorization': 'Bearer {}'.format(self.jwt)}
        )
        self.book_title = product_response.json()['title'] if product_response.status_code == 200 else self.book_title

        if claim_response.status_code == 200:
            logger.info('A new Packt Free Learning book has been grabbed!')
        elif claim_response.status_code == 409:
            logger.info('You have already claimed this offer.')
        else:
            logger.error('Claiming Packt Free Learning book has failed.')

    @login_required
    def download_books(self, titles=None, formats=None, into_folder=False):
        """
        Downloads the ebooks.
        :param titles: list('C# tutorial', 'c++ Tutorial') ;
        :param formats: tuple('pdf','mobi','epub','code');
        """
        def get_product_download_urls(product_id):
            error_message = 'Couldn\'t fetch download URLs for product {}.'.format(product_id)
            try:
                response = requests.get(
                    PACKT_API_PRODUCT_FILE_TYPES_URL.format(product_id=product_id),
                    headers={'authorization': 'Bearer {}'.format(self.jwt)}
                )
                if response.status_code == 200:
                    return {
                        format: PACKT_API_PRODUCT_FILE_DOWNLOAD_URL.format(product_id=product_id, file_type=format)
                        for format in response.json().get('data')[0].get('fileTypes')
                    }
                else:
                    logger.info(error_message)
                    return {}
            except Exception:
                raise PacktConnectionError(error_message)
        # download ebook
        my_books_data = self.get_all_books_data()
        if formats is None:
            formats = self.cfg.download_formats
            if formats is None:
                formats = self.download_formats
        if titles is not None:
            temp_book_data = [data for data in my_books_data
                              if any(slugify_book_title(data['title']) ==
                                     slugify_book_title(title) for title in
                                     titles)]
        else:  # download all
            temp_book_data = my_books_data
        if len(temp_book_data) == 0:
            logger.info("There is no books with provided titles: {} at your account!".format(titles))
        nr_of_books_downloaded = 0
        is_interactive = sys.stdout.isatty()
        for book in temp_book_data:
            download_urls = get_product_download_urls(book['id'])
            for format, download_url in download_urls.items():
                if format in formats:
                    file_extention = 'zip' if format == 'code' else format
                    title = slugify_book_title(book['title'])
                    logger.info("Title: '{}'".format(title))
                    if into_folder:
                        target_download_path = os.path.join(self.cfg.download_folder_path, title)
                        if not os.path.isdir(target_download_path):
                            os.mkdir(target_download_path)
                    else:
                        target_download_path = os.path.join(self.cfg.download_folder_path)
                    full_file_path = os.path.join(target_download_path, '{}.{}'.format(title, file_extention))
                    if os.path.isfile(full_file_path):
                        logger.info('"{}.{}" already exists under the given path.'.format(title, file_extention))
                    else:
                        if format == 'code':
                            logger.info("Downloading code for eBook: '{}'...".format(title))
                        else:
                            logger.info("Downloading eBook: '{}' in .{} format...".format(title, format))
                        try:
                            file_url = requests.get(
                                download_url,
                                headers={'authorization': 'Bearer {}'.format(self.jwt)}
                            ).json().get('data')
                            r = requests.get(
                                file_url,
                                headers={'authorization': 'Bearer {}'.format(self.jwt)},
                                timeout=100,
                                stream=True
                            )
                            if r.status_code is 200:
                                with open(full_file_path, 'wb') as f:
                                    total_length = int(r.headers.get('content-length'))
                                    num_of_chunks = (total_length / 1024) + 1
                                    for num, chunk in enumerate(r.iter_content(chunk_size=1024)):
                                        if chunk:
                                            if is_interactive:
                                                PacktPublishingFreeEbook.update_download_progress_bar(
                                                    num / num_of_chunks
                                                )
                                            f.write(chunk)
                                            f.flush()
                                    if is_interactive:
                                        PacktPublishingFreeEbook.update_download_progress_bar(-1)  # add end of line
                                if format == 'code':
                                    logger.success("Code for eBook: '{}' downloaded successfully!".format(title))
                                else:
                                    logger.success("eBook: '{}.{}' downloaded successfully!".format(title, format))
                                nr_of_books_downloaded += 1
                            else:
                                message = "Cannot download '{}'".format(title)
                                logger.error(message)
                                raise requests.exceptions.RequestException(message)
                        except Exception as e:
                            logger.error(e)
        logger.info("{} eBooks have been downloaded!".format(str(nr_of_books_downloaded)))

    @staticmethod
    def update_download_progress_bar(current_work_done):
        """Prints progress bar, current_work_done should be float value in range {0.0 - 1.0}, else prints '\n'"""
        if 0.0 <= current_work_done <= 1.0:
            print(
                "\r[PROGRESS] - [{0:50s}] {1:.1f}% ".format('#' * int(current_work_done * 50), current_work_done * 100),
                end="", )
        else:
            print("")


@click.command()
@click.option('-c', '--cfgpath', default=os.path.join(os.getcwd(), 'configFile.cfg'), help='Config file path.')
@click.option('-g', '--grab', is_flag=True, help='Grab Free Learning Packt ebook.')
@click.option('-gd', '--grabd', is_flag=True, help='Grab Free Learning Packt ebook and download it afterwards.')
@click.option('-da', '--dall', is_flag=True, help='Download all ebooks from your Packt account.')
@click.option('-sgd', '--sgd', is_flag=True, help='Grab Free Learning Packt ebook and download it to Google Drive.')
@click.option('-m', '--mail', is_flag=True, help='Grab Free Learning Packt ebook and send it by an email.')
@click.option('-sm', '--status_mail', is_flag=True, help='Send an email whether script execution was successful.')
@click.option('-f', '--folder', is_flag=True, help='Download ebooks into separate directories.')
@click.option(
    '--noauth_local_webserver',
    is_flag=True,
    default=False,
    help='See Google Drive API Setup section in README.'
)
def packt_cli(cfgpath, grab, grabd, dall, sgd, mail, status_mail, folder, noauth_local_webserver):
    into_folder = folder
    config_file_path = cfgpath

    try:
        cfg = ConfigurationModel(config_file_path)
        ebook = PacktPublishingFreeEbook(cfg)

        # Grab the newest book
        if grab or grabd or sgd or mail:
            ebook.grab_ebook()

            # Send email about successful book grab. Do it only when book
            # isn't going to be emailed as we don't want to send email twice.
            if status_mail and not mail:
                from utils.mail import MailBook
                mb = MailBook(config_file_path)
                mb.send_info(
                    subject=SUCCESS_EMAIL_SUBJECT.format(
                        dt.datetime.now().strftime(DATE_FORMAT),
                        ebook.book_title
                    ),
                    body=SUCCESS_EMAIL_BODY.format(ebook.book_title)
                )

        # Download book(s) into proper location.
        if grabd or dall or sgd or mail:
            if dall:
                ebook.download_books(into_folder=into_folder)
            elif grabd:
                ebook.download_books([ebook.book_title], into_folder=into_folder)
            else:
                cfg.download_folder_path = os.getcwd()
                ebook.download_books([ebook.book_title], into_folder=into_folder)

        # Send downloaded book(s) by mail or to Google Drive.
        if sgd or mail:
            paths = [
                os.path.join(cfg.download_folder_path, path)
                for path in os.listdir(cfg.download_folder_path)
                if os.path.isfile(path) and ebook.book_title in path
            ]
            if sgd:
                from utils.google_drive import GoogleDriveManager
                google_drive = GoogleDriveManager(config_file_path)
                google_drive.send_files(paths)
            else:
                from utils.mail import MailBook
                mb = MailBook(config_file_path)
                pdf_path = None
                mobi_path = None
                try:
                    pdf_path = [path for path in paths if path.endswith('.pdf')][-1]
                    mobi_path = [path for path in paths if path.endswith('.mobi')][-1]
                except IndexError:
                    pass
                if pdf_path:
                    mb.send_book(pdf_path)
                if mobi_path:
                    mb.send_kindle(mobi_path)
            for path in paths:
                os.remove(path)

        logger.success("Good, looks like all went well! :-)")
    except Exception as e:
        logger.error("Exception occurred {}".format(e))
        if status_mail:
            from utils.mail import MailBook
            mb = MailBook(config_file_path)
            mb.send_info(
                subject=FAILURE_EMAIL_SUBJECT.format(dt.datetime.now().strftime(DATE_FORMAT)),
                body=FAILURE_EMAIL_BODY.format(str(e))
            )
        sys.exit(2)
