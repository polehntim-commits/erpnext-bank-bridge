# SPDX-License-Identifier: MIT
"""Bank statements (v0.4.9).

The gap this closes: every opening balance Bank Bridge booked for an account
linked before v0.4.4 was ARITHMETIC — today's balance minus everything mirrored
since — and its accuracy was bounded by how far Plaid's transaction history
reached. Statements replace that with a number the bank itself wrote down, and
give every month a closing balance the mirror can be checked against.

Covered here:

  * the Plaid wrapper: /statements/list flattened across accounts, and the
    deliberate asymmetry where LISTING failures return [] (an Item without the
    product is the common case) while DOWNLOAD failures raise (Plaid promised a
    document and then withheld it)
  * the `statements` product is requested at Link time and FALLS BACK to a
    transactions-only token when Plaid refuses it — so linking a bank keeps
    working on an application that hasn't been approved for Statements
  * balance recovery from PDF text, in both the depository and the credit-card
    vocabulary, including negatives printed as '-' and as parentheses — and the
    NULL result for a layout we can't read, which every caller falls back on
  * storage: the {item}/{account}/{yyyy-mm}.pdf layout, path components that
    cannot escape the store, and atomic writes
  * idempotency on two levels — a statement already held is skipped without a
    download; a row whose PDF vanished is re-downloaded into the same row
  * exponential backoff on a failed download, and the row that survives a total
    failure so the next pull retries it
  * reconciliation: opening + mirrored movement vs the bank's closing balance,
    per account type, with 'no_data' held distinct from 'mismatch'
  * ANCHOR SAFETY — the check that makes this trustworthy: a statement is only
    booked from when the mirror can reproduce its closing balance AND holds no
    movement predating it
  * the boundary that keeps an upgrade safe: import FETCHES statements but does
    not change the balance it books; only the backfill path anchors

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import re
import tempfile
import unittest
import unittest.mock
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, crypto, db  # noqa: E402
from app import erpnext_accounts, erpnext_settings, plaid_settings  # noqa: E402
from app import opening_balance as obal  # noqa: E402
from app import account_visibility as av  # noqa: E402
from app import statements as stmts  # noqa: E402
from app.models import (BankTransaction, GeneratedJournalEntry,  # noqa: E402
                        PlaidAccount, PlaidItem, PlaidStatement)
from app.plaid_client import PlaidClient, PlaidError  # noqa: E402
from app.services import scheduler  # noqa: E402

from tests.fakes import FakeERPClient, FakePlaidClient  # noqa: E402

COMPANY = 'Example Company LLC'


# ── a real PDF, built by hand ────────────────────────────────────────────────
#
# Written out rather than pulled from a fixture file so the exact text under
# test is visible in the test that uses it — the parsing here is a recognizer
# for labels banks print, so what the page says IS the test input.

def make_pdf(lines) -> bytes:
    """A one-page PDF whose text layer is `lines`. Hand-assembled (objects,
    xref, trailer) so the suite needs no PDF-writing dependency to produce
    input that pypdf genuinely parses."""
    content = ['BT', '/F1 12 Tf', '72 720 Td', '14 TL']
    for line in lines:
        esc = line.replace('\\', r'\\').replace('(', r'\(').replace(')', r'\)')
        content.append(f'({esc}) Tj T*')
    content.append('ET')
    stream = '\n'.join(content).encode('latin-1')
    objs = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
        b'/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream
        + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f'{i} 0 obj\n'.encode() + body + b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objs) + 1}\n'.encode()
    out += b'0000000000 65535 f \n'
    for off in offsets:
        out += f'{off:010d} 00000 n \n'.encode()
    out += (f'trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n'
            f'startxref\n{xref}\n%%EOF\n').encode()
    return bytes(out)


def checking_pdf(opening='17,600.00', closing='17,650.00') -> bytes:
    """A depository statement in the vocabulary a bank actually prints."""
    return make_pdf([
        'WELLS FARGO EVERYDAY CHECKING',
        'Statement period July 1, 2026 - July 31, 2026',
        f'Beginning balance on July 1        ${opening}',
        'Deposits and other credits          1,200.00',
        f'Ending balance on July 31          ${closing}',
    ])


def wf_advisors_pdf(cash_open='255,038.26', cash_close='39,751.95',
                    port_open='1,360,707.12', port_close='1,313,136.16',
                    with_sweep_table=True, with_holdings=False,
                    with_prose=False) -> bytes:
    """A Wells Fargo Advisors brokerage statement, anonymised.

    Every line here is transcribed from a real statement on the live install
    (account numbers, names and addresses removed), because the layout is the
    whole point of the v0.4.41 parser and a plausible-looking invention would
    not have caught the bugs that motivated it. Note in particular:

      * 'Opening value' appears TWICE with different numbers — once in the
        Progress summary as total account value, once in the Cash flow summary
        qualified by 'of cash and sweep balances'
      * every summary line carries a SECOND column (this-year-to-date) that
        must not be mistaken for the period figure, and the gain/loss roll-up
        carries THREE
      * the Cash sweep activity table is one trap: pypdf flattens it into text
        where 'ENDING BALANCE' is immediately followed by a sweep transfer's
        amount, which v0.4.40 read as the month's closing balance
      * `with_holdings` adds the second trap — the holdings tables, whose ~90
        rows each begin with the word 'Total', same as the gain/loss roll-up
      * `with_prose` adds the third — page 2's disclosure paragraph, which
        opens with the words 'Income summary:'
    """
    lines = [
        'SNAPSHOT',
        'Current period ending June 30, 2026',
        'ACCOUNT NAME: EXAMPLE COMPANY LLC',
        'Wells Fargo Advisors is a trade name used by Wells Fargo Clearing '
        'Services, LLC',
    ]
    if with_prose:
        lines += [
            'Income summary: The Income summary displays all income as '
            'recorded in the tax system as of period end date. Available '
            'without charge upon request. Totals may not match 1,234.56 due '
            'to reclassifications made in the tax system.',
            'Gain/loss summary: This statement presents estimated unrealized '
            'or realized gains or losses for your information only, 9,999.99, '
            'and should not be relied upon for tax reporting purposes.',
        ]
    lines += [
        'Progress summary Value over time',
        'THIS PERIOD THIS YEAR 1,748,000',
        f'Opening value ${port_open} $1,576,555.19',
        'Cash deposited 10,000.00 40,000.00 1,311,000Securities deposited '
        '0.00 0.00',
        'Cash withdrawn -20,047.16 -327,309.02',
        'Securities withdrawn 0.00 0.00 874,000Change in value -37,523.80 '
        '23,889.99',
        f'Closing value ${port_close} ${port_close} 437,000',
        'Portfolio summary',
        'CURRENT ASSET TYPE VALUE ON MAY 31 % VALUE ON JUN 30 % ANN. INCOME',
        f'Asset value ${port_open} 100% ${port_close} 100% $20,804',
        'Cash flow summary THIS PERIOD THIS YEAR',
        f'Opening value of cash and sweep balances ${cash_open}',
        'Income and distributions 1,656.03 6,956.44',
        'Securities sold and redeemed 131,931.66 1,533,107.85',
        'Other additions 10,000.00 40,000.00',
        'Net additions to cash $143,587.69 $1,580,064.29',
        'Securities purchased -338,826.84 -1,222,655.62',
        'Advisory, manager and platform fees 0.00 -7,181.51',
        'Other subtractions, transfers & charges -20,047.16 -320,127.51',
        'Net subtractions from cash -$358,874.00 -$1,549,964.64',
        f'Closing value of cash and sweep balances ${cash_close}',
        'Income summary * THIS PERIOD THIS YEAR',
        'Taxable money market/sweep funds 75.78 616.13',
        'Interest 980.73 11,714.50',
        'Ordinary dividends and ST capital gains 434.29 1,879.58',
        'Qualified dividends 1,145.96 4,301.70',
        'Other 0.00 19.88',
        'Total taxable income $2,636.76 $18,531.79',
        'Total federally tax-exempt income $0.00 $0.00',
        'Total income $2,636.76 $18,531.79',
        'Gain/loss summary UNREALIZED THIS PERIOD REALIZED THIS YEAR REALIZED',
        'Short term (S) -4,180.15 -3,968.19 -5,053.13',
        'Long term (L) 48,792.11 1,039.76 2,389.26',
        'Total $44,611.96 -$2,928.43 -$2,663.87',
        f'Your total available funds ${cash_close}',
        'ORCHARD EXAMPLE LLC',
        'June 1, 2026 - June 30, 2026',
    ]
    if with_holdings:
        lines += ['Stocks and exchange-traded products',
                  'DESCRIPTION % QUANTITY PRICE COST BASIS VALUE GAIN/LOSS']
        lines += [f'Total 0.8{n} 750 $14.15 $10,611.53 14.1300 $10,597.50 '
                  f'-$14.03 n/a n/a' for n in range(9)]
    if with_sweep_table:
        lines += [
            'Cash sweep activity',
            'BEGINNING BALANCE TRANSFER FROM BANK DEPOSIT SWEEP06/01 '
            '255,038.26 06/18 -75,594.81',
            'TRANSFER TO BANK DEPOSIT SWEEP ENDING BALANCE06/16 100,000.00 '
            f'06/30 {cash_close}',
            'DATE TRANSACTION DESCRIPTION AMOUNT BANK BALANCE',
            '06/01 BEGINNING BALANCE $0.00',
            '06/30 ENDING BALANCE $0.00',
        ]
    return make_pdf(lines)


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')


def fixture_pdf(name: str) -> bytes:
    """A synthetic statement fixture, rendered into a real PDF.

    The fixtures are HAND-WRITTEN text (app/tests/fixtures/*_synthetic.txt),
    never the output of running pypdf over a real statement: that output
    carries the account holder's address, personal email, the advisor's name
    and direct line, the account number and every transaction with merchant and
    card last-4, in a form that greps more cleanly than the PDF does. Only the
    LABELS and DOLLAR FIGURES are verbatim, because those are the parser's
    behaviour rather than anyone's identity. See .gitignore, which refuses both
    the PDFs and anything named *_extracted.txt.

    Rendering to a PDF and back — rather than feeding the text straight in —
    is deliberate: it exercises extract_lines and the pypdf round trip, so a
    fixture cannot pass through a code path the real documents skip."""
    with open(os.path.join(FIXTURES, name), encoding='utf-8') as fh:
        lines = [l.rstrip('\n') for l in fh
                 if l.strip() and not l.lstrip().startswith('#')]
    return make_pdf(lines)


def wf_advisors_june_2025_pdf() -> bytes:
    """Self-directed brokerage holding only swept cash — the statement whose
    row the live install was showing a closing balance of -10.78 for."""
    return fixture_pdf('wf_advisors_snapshot_synthetic.txt')


def wf_advisors_managed_pdf() -> bytes:
    """ASSET ADVISOR managed brokerage holding securities — the second
    account, whose statement prints a materially different vocabulary."""
    return fixture_pdf('wf_advisors_managed_synthetic.txt')


def wf_deposit_pdf() -> bytes:
    """A Wells Fargo business checking statement.

    UNLIKE `wf_advisors_pdf` this is NOT transcribed from a real document — the
    live install holds no real deposit statement, only Plaid sandbox mocks
    ('First Platypus Bank', 'Balance on XX/XX:') with no labelled totals at
    all. It follows the standard Wells Fargo Activity summary layout, and the
    parser marks figures from it `verified: False` for that reason."""
    return make_pdf([
        'WELLS FARGO BUSINESS CHOICE CHECKING',
        'Account number: ****1234',
        'Statement period July 1, 2026 - July 31, 2026',
        'Activity summary',
        'Beginning balance on July 1 $17,600.00',
        'Deposits and other credits 1,200.00',
        'Withdrawals and other debits -1,138.00',
        'Checks paid -450.00',
        'Ending balance on July 31 $17,650.00',
        'Average ledger balance $17,412.88',
        'Average collected balance $17,388.10',
        'Interest earned this statement period 0.42',
        'Total service fees -12.00',
        'Transaction history',
        '07/03 Deposit Cherry sale 1,200.00 18,800.00',
        '07/09 Check No. 1042 -450.00 18,350.00',
    ])


def wf_card_pdf() -> bytes:
    """A Wells Fargo business credit card statement.

    Also NOT transcribed from a real document — see `wf_deposit_pdf`."""
    return make_pdf([
        'WELLS FARGO BUSINESS ELITE SIGNATURE CARD',
        'Account ending in 9999',
        'Statement period 07/01/2026 - 07/31/2026',
        'Account summary',
        'Previous balance $2,400.00',
        'Payments and credits -1,200.00',
        'Purchases and adjustments 1,425.50',
        'Cash advances 0.00',
        'Fees charged 12.00',
        'Interest charged 12.50',
        'New balance total $2,650.00',
        'Total credit limit $25,000.00',
        'Available credit $22,350.00',
        'Minimum payment due $53.00',
        'Payment due date August 22, 2026',
    ])


def card_pdf(opening='2,400.00', closing='2,650.00') -> bytes:
    """A credit-card statement, which uses an entirely different vocabulary —
    'previous balance' / 'new balance' rather than beginning/ending."""
    return make_pdf([
        'CHASE SAPPHIRE STATEMENT',
        f'Previous balance      ${opening}',
        'Purchases               250.00',
        f'New balance           ${closing}',
    ])


class StatementsBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self._store = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'STATEMENTS_STORAGE_PATH': self._store,
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', COMPANY)
        self.item = self._item()
        # Never actually sleep between download retries.
        patcher = unittest.mock.patch.object(stmts, '_sleep', lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _item(self, item_id='item-abc'):
        it = PlaidItem(item_id=item_id,
                       access_token_encrypted=crypto.encrypt('access-x'),
                       institution_id='ins_1', institution_name='Wells Fargo',
                       status='active')
        db.session.add(it)
        db.session.commit()
        return it

    def _account(self, account_id='acct-1', type_='depository',
                 subtype='checking', balance=1000.0,
                 gl='Wells Fargo Checking - EC', item_id='item-abc'):
        a = PlaidAccount(
            account_id=account_id, item_id=item_id, name=f'{subtype} 1234',
            mask='1234', type=type_, subtype=subtype, balance_current=balance,
            iso_currency_code='USD', erpnext_gl_account_name=gl,
            erpnext_bank_account_name='BA-1', import_status='imported')
        db.session.add(a)
        db.session.commit()
        return a

    def _txn(self, txn_id, account_id='acct-1', amount=0.0, when=None,
             pending=False, removed=False):
        t = BankTransaction(plaid_transaction_id=txn_id, account_id=account_id,
                            amount=amount, date=when or date(2026, 7, 10),
                            name='TXN', pending=pending, removed=removed)
        db.session.add(t)
        db.session.commit()
        return t

    def _statement(self, statement_id='st-1', account_id='acct-1',
                   month=7, year=2026, opening=17600.0, closing=17650.0,
                   pdf=True):
        start, end = stmts.period_bounds(month, year)
        path = ''
        size = 0
        if pdf:
            path = stmts.pdf_path_for('item-abc', account_id,
                                      stmts.period_label(start), statement_id)
            size = stmts.store_pdf(path, checking_pdf())
        st = PlaidStatement(statement_id=statement_id, plaid_item_id='item-abc',
                            plaid_account_id=account_id, period_start=start,
                            period_end=end, opening_balance=opening,
                            closing_balance=closing, pdf_path=path,
                            pdf_bytes=size)
        db.session.add(st)
        db.session.commit()
        return st

    @staticmethod
    def _listed(statement_id='st-1', account_id='acct-1', month=7, year=2026):
        return {'account_id': account_id, 'statement_id': statement_id,
                'month': month, 'year': year, 'date_posted': None}

    def _erp(self, **kw):
        # An Equity root for the Opening Balance Equity leaf, plus the Assets
        # branch the imported account's own GL leaf is created under — the
        # import path needs both before it books anything.
        kw.setdefault('chart_accounts', [
            {'account_name': 'Equity', 'is_group': 1, 'root_type': 'Equity',
             'parent_account': ''},
            {'account_name': 'Application of Funds (Assets)', 'is_group': 1,
             'root_type': 'Asset', 'parent_account': ''},
            {'account_name': 'Current Assets', 'is_group': 1,
             'root_type': 'Asset',
             'parent_account': 'Application of Funds (Assets) - EC'},
            {'account_name': 'Bank Accounts', 'is_group': 1,
             'root_type': 'Asset', 'parent_account': 'Current Assets - EC'},
        ])
        kw.setdefault('companies', [COMPANY])
        return FakeERPClient(**kw)


# ── the Plaid wrapper ────────────────────────────────────────────────────────

class StatementsAPITest(StatementsBase):
    """The dict-in/dict-out surface over Plaid's Statements endpoints."""

    def test_statements_list_flattens_accounts(self):
        """/statements/list nests statements under each account; the wrapper
        flattens them and carries the owning account_id onto every row, because
        that is the key everything downstream files a PDF under."""
        api = unittest.mock.Mock()
        api.statements_list.return_value = {'accounts': [
            {'account_id': 'acct-1', 'statements': [
                {'statement_id': 'st-a', 'month': 6, 'year': 2026,
                 'date_posted': date(2026, 7, 2)},
                {'statement_id': 'st-b', 'month': 7, 'year': 2026,
                 'date_posted': None}]},
            {'account_id': 'acct-2', 'statements': [
                {'statement_id': 'st-c', 'month': 7, 'year': 2026}]},
        ]}
        rows = PlaidClient(api=api).statements_list('access-x')
        self.assertEqual(
            [(r['account_id'], r['statement_id'], r['month']) for r in rows],
            [('acct-1', 'st-a', 6), ('acct-1', 'st-b', 7),
             ('acct-2', 'st-c', 7)])
        self.assertEqual(rows[0]['date_posted'], '2026-07-02')
        self.assertIsNone(rows[1]['date_posted'])

    def test_statements_list_returns_empty_for_unsupported_bank(self):
        """An Item without the Statements product is the COMMON case, not an
        error — the product must be approved on the application, requested at
        Link, and offered by the institution. Returning [] lets every caller
        fall back to v0.4.4 behaviour instead of breaking."""
        api = unittest.mock.Mock()
        api.statements_list.side_effect = Exception(
            'PRODUCT_NOT_READY: statements is not enabled for this Item')
        self.assertEqual(PlaidClient(api=api).statements_list('access-x'), [])

    def test_statements_list_skips_rows_with_no_id(self):
        api = unittest.mock.Mock()
        api.statements_list.return_value = {'accounts': [
            {'account_id': 'acct-1',
             'statements': [{'month': 7, 'year': 2026}, {'statement_id': 'ok'}]}]}
        rows = PlaidClient(api=api).statements_list('access-x')
        self.assertEqual([r['statement_id'] for r in rows], ['ok'])

    def test_statements_download_returns_bytes(self):
        api = unittest.mock.Mock()
        api.statements_download.return_value = b'%PDF-1.4 hello'
        self.assertEqual(PlaidClient(api=api).statements_download('a', 'st-1'),
                         b'%PDF-1.4 hello')

    def test_statements_download_reads_a_file_like_response(self):
        """The SDK types this endpoint's return as `file_type`, so what comes
        back is a file object rather than bytes."""
        import io
        api = unittest.mock.Mock()
        api.statements_download.return_value = io.BytesIO(b'%PDF-bytes')
        self.assertEqual(PlaidClient(api=api).statements_download('a', 'st-1'),
                         b'%PDF-bytes')

    def test_statements_download_raises_rather_than_returning_empty(self):
        """UNLIKE listing: Plaid said this exact statement exists and then
        wouldn't hand it over. Swallowing that would store an empty PDF."""
        api = unittest.mock.Mock()
        api.statements_download.side_effect = Exception('boom')
        with self.assertRaises(PlaidError):
            PlaidClient(api=api).statements_download('a', 'st-1')
        api.statements_download.side_effect = None
        api.statements_download.return_value = b''
        with self.assertRaises(PlaidError):
            PlaidClient(api=api).statements_download('a', 'st-1')


class LinkTokenStatementsTest(StatementsBase):
    """Requesting the `statements` product at Link time, and surviving a Plaid
    that hasn't approved it."""

    def test_statements_product_is_requested_when_asked(self):
        api = unittest.mock.Mock()
        api.link_token_create.return_value = {'link_token': 'link-1'}
        token = PlaidClient(api=api).create_link_token('u', statements=True)
        self.assertEqual(token, 'link-1')
        req = api.link_token_create.call_args[0][0]
        self.assertIn('statements', [str(p.value) for p in req.products])
        self.assertTrue(hasattr(req, 'statements'))

    def test_link_token_falls_back_without_statements_when_plaid_refuses(self):
        """THE COMPATIBILITY GUARANTEE: an application not yet approved for
        Statements must keep linking banks. The first attempt is rejected, the
        retry drops the product, and the operator gets a working token."""
        api = unittest.mock.Mock()
        calls = []

        def _create(req):
            calls.append(req)
            if len(calls) == 1:
                raise Exception('INVALID_PRODUCT: statements is not enabled')
            return {'link_token': 'link-fallback'}

        api.link_token_create.side_effect = _create
        token = PlaidClient(api=api).create_link_token('u', statements=True)
        self.assertEqual(token, 'link-fallback')
        self.assertEqual(len(calls), 2)
        self.assertEqual([str(p.value) for p in calls[1].products],
                         ['transactions'])

    def test_statements_not_requested_when_off(self):
        api = unittest.mock.Mock()
        api.link_token_create.return_value = {'link_token': 'link-1'}
        PlaidClient(api=api).create_link_token('u', statements=False)
        req = api.link_token_create.call_args[0][0]
        self.assertEqual([str(p.value) for p in req.products], ['transactions'])


# ── PDF parsing ──────────────────────────────────────────────────────────────

class ParseBalancesTest(StatementsBase):
    """Recovering the two numbers Plaid only ever puts inside the PDF."""

    def test_depository_statement(self):
        got = stmts.parse_balances(checking_pdf())
        self.assertEqual(got['opening'], 17600.00)
        self.assertEqual(got['closing'], 17650.00)
        self.assertTrue(got['has_text'])

    def test_credit_card_vocabulary(self):
        """A card statement says 'previous balance' / 'new balance' and never
        the words 'beginning' or 'ending'."""
        got = stmts.parse_balances(card_pdf())
        self.assertEqual(got['opening'], 2400.00)
        self.assertEqual(got['closing'], 2650.00)

    def test_negative_balances_in_both_printed_forms(self):
        """An overdrawn account prints '-1,234.56'; many statements print the
        same thing as '(1,234.56)'. Both mean negative."""
        minus = stmts.parse_balances(make_pdf([
            'Beginning balance -1,234.56', 'Ending balance -99.00']))
        self.assertEqual(minus['opening'], -1234.56)
        self.assertEqual(minus['closing'], -99.00)
        parens = stmts.parse_balances(make_pdf([
            'Beginning balance (1,234.56)', 'Ending balance (99.00)']))
        self.assertEqual(parens['opening'], -1234.56)
        self.assertEqual(parens['closing'], -99.00)

    def test_unrecognized_layout_yields_none_not_a_guess(self):
        """The whole safety property: a layout we can't read produces NULL, and
        every consumer falls back to the v0.4.4 estimate."""
        got = stmts.parse_balances(make_pdf([
            'MYSTERY BANK', 'Some numbers 1,234.56 and 99.00']))
        self.assertIsNone(got['opening'])
        self.assertIsNone(got['closing'])
        self.assertTrue(got['has_text'])

    def test_image_only_or_broken_pdf_is_not_an_error(self):
        """A scanned statement has no text layer; garbage isn't a PDF at all."""
        for data in (b'', b'not a pdf at all', make_pdf([])):
            got = stmts.parse_balances(data)
            self.assertIsNone(got['opening'])
            self.assertIsNone(got['closing'])

    def test_amount_search_is_windowed_to_its_own_label(self):
        """Extracted text runs a statement's columns together, so an unbounded
        search would let one label claim a figure printed far below it."""
        far = 'x' * 200
        got = stmts.parse_balances(make_pdf([f'Beginning balance {far} 5,000.00']))
        self.assertIsNone(got['opening'])


# ── the layout the live install actually receives (v0.4.41) ──────────────────

class WellsFargoAdvisorsParseTest(StatementsBase):
    """A real brokerage statement, and the misparse that motivated v0.4.41.

    Up to v0.4.40 the parser worked on one flat string and took the first
    amount within 120 characters of a label, which read a cash-sweep TRANSFER
    as the month's closing balance on all 26 Wells Fargo statements the live
    install holds. A wrong bank-asserted figure is worse than a missing one —
    it is the input to reconciliation and to choose_anchor_statement, i.e. to a
    posted opening balance — so these tests pin both what is now recovered and
    what is now refused."""

    def test_cash_and_sweep_balances_are_the_reconcilable_figures(self):
        """opening/closing take the CASH side, not total account value: the
        mirror sums cash events and has no record of market movement, so cash
        is the only figure reconciliation can reproduce."""
        got = stmts.parse_balances(wf_advisors_pdf())
        self.assertEqual(got['opening'], 255038.26)
        self.assertEqual(got['closing'], 39751.95)
        self.assertEqual(got['method'], 'wf_advisors')

    def test_portfolio_value_is_kept_apart_from_the_cash_balance(self):
        """'Opening value' and 'Opening value of cash and sweep balances' are
        two different numbers on the same page. Confusing them would book a
        $1.3M opening balance for an account holding $40k of cash."""
        got = stmts.parse_balances(wf_advisors_pdf())
        self.assertEqual(got['portfolio_opening'], 1360707.12)
        self.assertEqual(got['portfolio_closing'], 1313136.16)

    def test_sweep_activity_table_cannot_claim_the_closing_balance(self):
        """THE v0.4.40 BUG. pypdf flattens the sweep table so that 'ENDING
        BALANCE' is followed immediately by '06/16 100,000.00' — a single
        transfer. The digits in the gap are what disqualify it."""
        got = stmts.parse_balances(wf_advisors_pdf())
        self.assertNotEqual(got['closing'], 100000.00)
        self.assertNotEqual(got['opening'], 255038.26 * 0 + 15434.91)
        # …and the sweep table's own $0.00 rows, which open with a MM/DD and
        # are therefore transaction rows rather than summary lines.
        self.assertNotEqual(got['closing'], 0.00)

    def test_second_column_is_year_to_date_not_this_period(self):
        """Both summary lines carry a this-year column after the period one.
        The first amount on the line is the period figure."""
        got = stmts.parse_balances(wf_advisors_pdf(port_open='9,467.48'))
        self.assertEqual(got['portfolio_opening'], 9467.48)
        self.assertNotEqual(got['portfolio_opening'], 1576555.19)

    def test_every_brokerage_figure_is_captured_as_documentation(self):
        """The v0.4.41 point: a statement can stand on its own as support for a
        journal entry only if everything it asserts was recovered, not just the
        two balances. Each of these is transcribed from a production PDF."""
        m = stmts.parse_statement(wf_advisors_pdf())['metadata']
        expected = {
            'cash_opening': 255038.26, 'cash_closing': 39751.95,
            'portfolio_opening': 1360707.12, 'portfolio_closing': 1313136.16,
            'deposits_total': 10000.00, 'withdrawals_total': -20047.16,
            'change_in_value': -37523.80,
            'securities_purchased': -338826.84,
            'securities_sold': 131931.66,
            'income_distributions': 1656.03,
            'net_additions': 143587.69,
            'net_subtractions': -358874.00,
            'other_additions': 10000.00, 'other_subtractions': -20047.16,
            'advisory_fees': 0.00,
            'interest_income': 980.73, 'sweep_income': 75.78,
            'ordinary_dividends': 434.29, 'qualified_dividends': 1145.96,
            'dividends_total': 1580.25,
            'total_taxable_income': 2636.76, 'tax_exempt_income': 0.00,
            'total_income': 2636.76,
            'available_funds': 39751.95,
        }
        for key, want in expected.items():
            self.assertEqual(m.get(key), want, key)
        self.assertEqual(m['fields_failed'], [])

    def test_the_gain_loss_columns_are_read_by_position(self):
        """The roll-up line is literally 'Total' and carries three columns —
        unrealized, realized this period, realized year to date."""
        m = stmts.parse_statement(wf_advisors_pdf())['metadata']
        self.assertEqual(m['total_gainloss'], {
            'unrealized': 44611.96, 'realized_period': -2928.43,
            'realized_ytd': -2663.87})
        self.assertEqual(m['short_term_gainloss'], {
            'unrealized': -4180.15, 'realized_period': -3968.19,
            'realized_ytd': -5053.13})
        self.assertEqual(m['long_term_gainloss'], {
            'unrealized': 48792.11, 'realized_period': 1039.76,
            'realized_ytd': 2389.26})

    def test_a_holdings_table_cannot_answer_for_the_gain_loss_summary(self):
        """~90 holdings rows also begin with 'Total'. Section scoping is what
        stops one of them being read as the account's realized gain."""
        m = stmts.parse_statement(wf_advisors_pdf(with_holdings=True))['metadata']
        self.assertEqual(m['total_gainloss']['unrealized'], 44611.96)
        self.assertEqual(m['total_gainloss']['realized_period'], -2928.43)

    def test_prose_mentioning_a_section_is_not_that_section(self):
        """Page 2 of every WF statement carries 'Income summary: The Income
        summary displays all income as recorded in the tax system…'. Scoping to
        that paragraph would make every income figure wrong."""
        m = stmts.parse_statement(wf_advisors_pdf(with_prose=True))['metadata']
        self.assertEqual(m['total_income'], 2636.76)
        self.assertEqual(m['interest_income'], 980.73)

    def test_the_period_the_statement_states_is_recovered(self):
        """Plaid gives only month+year, i.e. the CALENDAR month. What the
        document says about its own period is the only record of a cycle that
        doesn't align to one."""
        m = stmts.parse_statement(wf_advisors_pdf())['metadata']
        self.assertEqual(m['period_start'], '2026-06-01')
        self.assertEqual(m['period_end'], '2026-06-30')

    def test_the_parse_records_its_own_provenance(self):
        m = stmts.parse_statement(wf_advisors_pdf())['metadata']
        self.assertEqual(m['parser_version'], stmts.PARSER_VERSION)
        self.assertEqual(m['layout'], 'wf_advisors')
        self.assertTrue(m['verified'])

    def test_one_bad_field_costs_one_figure_not_the_whole_blob(self):
        """Defensive extraction, per Tim's requirement: these tables will grow
        and most describe layouts nobody here has seen, so a pattern that
        raises must cost its own figure and be NAMED — not silently take the
        statement's balances down with it."""
        broken = (('boom', '', ('x',), 0, ()),) + stmts._WF_ADVISORS_FIELDS

        def explode(lines, labels, exclude=()):
            if labels == ('x',):
                raise RuntimeError('bad pattern')
            return real(lines, labels, exclude=exclude)

        real = stmts._find_amounts
        with unittest.mock.patch.dict(stmts._LAYOUT_FIELDS,
                                      {'wf_advisors': broken}), \
             unittest.mock.patch.object(stmts, '_find_amounts', explode):
            got = stmts.parse_statement(wf_advisors_pdf())
        self.assertEqual(got['opening'], 255038.26)
        self.assertEqual(got['closing'], 39751.95)
        self.assertEqual(got['metadata']['fields_failed'], ['boom'])


class June2025GroundTruthTest(StatementsBase):
    """The production statement that exposed the whole problem.

    Account ...6030, June 2025. The live install was showing this row with a
    closing balance of **-10.78** — the amount of one cash-sweep transfer,
    which the v0.4.40 recognizer picked up because 'ENDING BALANCE' happens to
    be followed by it in the flattened text. The real closing balance is
    7,793.51. Every figure asserted here was read off that document."""

    EXPECTED = {
        'cash_opening': 9467.48,
        'income_distributions': 0.34,
        'other_additions': 0.00,
        'net_additions': 0.34,
        'check_withdrawals': 0.00,
        'atm_activity': -1674.31,
        'other_subtractions': 0.00,
        'net_subtractions': -1674.31,
        'cash_closing': 7793.51,
        'sweep_income': 0.34,
        'total_taxable_income': 0.34,
        'tax_exempt_income': 0.00,
        'total_income': 0.34,
        'portfolio_opening': 9467.48,
        'portfolio_closing': 7793.51,
        'available_funds': 7793.51,
    }

    def test_every_snapshot_figure(self):
        m = stmts.parse_statement(wf_advisors_june_2025_pdf())['metadata']
        for key, want in self.EXPECTED.items():
            self.assertEqual(m.get(key), want, key)
        self.assertEqual(m['fields_failed'], [])

    def test_the_closing_balance_is_not_the_sweep_transfer(self):
        """THE REGRESSION. -10.78 is a single transfer out of the sweep; the
        month closed at 7,793.51."""
        got = stmts.parse_statement(wf_advisors_june_2025_pdf())
        self.assertEqual(got['closing'], 7793.51)
        self.assertNotEqual(got['closing'], -10.78)
        self.assertEqual(got['opening'], 9467.48)

    def test_an_all_zero_gain_loss_summary_is_still_three_columns(self):
        m = stmts.parse_statement(wf_advisors_june_2025_pdf())['metadata']
        zero = {'unrealized': 0.0, 'realized_period': 0.0, 'realized_ytd': 0.0}
        for key in ('short_term_gainloss', 'long_term_gainloss',
                    'total_gainloss'):
            self.assertEqual(m[key], zero, key)

    def test_the_column_header_glued_to_the_income_label_still_matches(self):
        """The line reads 'TAXABLE Money market/sweep funds 0.34 1.60' — the
        section's column header runs into the label. Matching only the
        prefixed form would work here and fail on a statement that prints the
        label alone, so both are listed."""
        self.assertEqual(
            stmts.parse_statement(wf_advisors_june_2025_pdf())
            ['metadata']['sweep_income'], 0.34)
        bare = make_pdf([
            'Cash flow summary THIS PERIOD THIS YEAR',
            'Opening value of cash and sweep balances $9,467.48',
            'Closing value of cash and sweep balances $7,793.51',
            'Income summary * THIS PERIOD THIS YEAR',
            'Money market/sweep funds 0.34 1.60',
            'Total income $0.34 $1.60',
        ])
        self.assertEqual(stmts.parse_statement(bare)
                         ['metadata']['sweep_income'], 0.34)

    def test_the_page_inventory_records_what_is_not_mined(self):
        """A production statement is 9+ pages and this parser reads the
        summary. The inventory is how a decision about mining the rest gets
        made from data rather than by opening PDFs by hand."""
        m = stmts.parse_statement(wf_advisors_june_2025_pdf())['metadata']
        self.assertTrue(m['pages'])
        page = m['pages'][0]
        self.assertEqual(page['page'], 1)
        self.assertIn('lines', page)
        self.assertIn('heading', page)


class ManagedBrokerageGroundTruthTest(StatementsBase):
    """The SECOND production account — ASSET ADVISOR managed, ~$1.5M, holding
    securities. Account ...9401, June 2025.

    Same institution, materially different statement: the cash flow summary
    gains four lines the cash-only account never prints, the income summary
    gains four more, the gain/loss rows are labelled differently, and two
    non-numeric fields appear that mark the account as managed. Every figure
    here was read off that document."""

    EXPECTED = {
        'cash_opening': 3507.75,
        'cash_closing': 9702.20,
        'portfolio_opening': 1539523.99,
        'portfolio_closing': 1557537.86,
        'income_distributions': 1121.79,
        'securities_sold': 24705.10,
        'securities_purchased': -19587.94,
        'advisory_fees': 0.00,
        'interest_income': 0.00,
        'ordinary_dividends': 218.77,
        'qualified_dividends': 885.38,
        'other_income': 0.00,
        'total_taxable_income': 1121.79,
        'tax_exempt_income': 0.00,
        'total_income': 1121.79,
        'net_additions': 25826.89,
        'net_subtractions': -19632.44,
        'other_additions': 0.00,
        'other_subtractions': -44.50,
        'gross_proceeds': 24705.10,
        'foreign_withholding': -40.80,
        'available_funds': 9702.20,
    }

    def test_every_snapshot_figure(self):
        m = stmts.parse_statement(wf_advisors_managed_pdf())['metadata']
        for key, want in self.EXPECTED.items():
            self.assertEqual(m.get(key), want, key)
        self.assertEqual(m['fields_failed'], [])

    def test_the_gain_loss_grid(self):
        """'Short term (S)' here vs 'Short term/Net lots' on the cash-only
        account — the (S)/(L) are tax-lot designators the parser tolerates."""
        m = stmts.parse_statement(wf_advisors_managed_pdf())['metadata']
        self.assertEqual(m['short_term_gainloss'], {
            'unrealized': 27499.81, 'realized_period': 268.08,
            'realized_ytd': 2388.87})
        self.assertEqual(m['long_term_gainloss'], {
            'unrealized': 7486.80, 'realized_period': 0.00,
            'realized_ytd': 3377.61})
        self.assertEqual(m['total_gainloss'], {
            'unrealized': 34986.61, 'realized_period': 268.08,
            'realized_ytd': 5766.48})

    def test_electronic_funds_transfers_are_split_by_position(self):
        """The label is printed TWICE with identical wording, once under
        additions and once under subtractions — the sign lives in the position.
        Reporting whichever came first would call an outflow an inflow on any
        month where the two differ, which is the month it would matter."""
        m = stmts.parse_statement(wf_advisors_managed_pdf())['metadata']
        self.assertEqual(m['electronic_transfers_in'], 0.00)
        self.assertEqual(m['electronic_transfers_out'], 0.00)
        # …and the YEAR column proves the two rows really are distinct.
        self.assertNotIn('electronic_transfers', m)

    def test_a_bare_other_label_reads_its_own_row(self):
        """'Other' is a substring of 'Other additions', 'Other subtractions'
        and most disclosure prose. Whole-word matching is what makes the income
        summary's bare row safe to name."""
        m = stmts.parse_statement(wf_advisors_managed_pdf())['metadata']
        self.assertEqual(m['other_income'], 0.00)
        self.assertEqual(m['other_additions'], 0.00)
        self.assertEqual(m['other_subtractions'], -44.50)

    def test_the_advisory_program_marks_a_managed_account(self):
        """Presence is the signal — a self-directed brokerage prints neither.
        Strings, not numbers: '1.00%' is a disclosed rate to reproduce
        verbatim, not something to compute with."""
        m = stmts.parse_statement(wf_advisors_managed_pdf())['metadata']
        self.assertEqual(m['advisory_program'], 'ASSET ADVISOR')
        self.assertEqual(m['advisory_fee_rate'], '1.00%')
        cash_only = stmts.parse_statement(
            wf_advisors_june_2025_pdf())['metadata']
        self.assertNotIn('advisory_program', cash_only)
        self.assertNotIn('advisory_fee_rate', cash_only)

    def test_the_holdings_table_still_cannot_answer_for_gain_loss(self):
        """This fixture carries real holdings rows, every one beginning
        'Total'."""
        m = stmts.parse_statement(wf_advisors_managed_pdf())['metadata']
        self.assertEqual(m['total_gainloss']['unrealized'], 34986.61)


class StatementAnchorTest(StatementsBase):
    """Statement-anchored reconciliation (v0.4.43).

    Bank Bridge's own record of what each account held at each statement
    boundary — deliberately independent of ERPNext, because the accounts this
    matters most for belong to a second entity whose instance does not exist
    yet. Posting corrections into the first entity's books today would be work
    to reverse tomorrow, so this release emits nothing."""

    def setUp(self):
        super().setUp()
        self.client_ = self.app.test_client()
        self.cash = self._acct('acct-6030', 'Business Brokerage', '6030')
        self.managed = self._acct('acct-9401', 'Asset Advisor', '9401')

    def _acct(self, account_id, name, mask):
        a = PlaidAccount(account_id=account_id, item_id=self.item.item_id,
                         name=name, mask=mask, type='investment',
                         subtype='brokerage')
        db.session.add(a)
        db.session.commit()
        return a

    def _anchor(self, account, pdf, statement_id, start, end):
        st = PlaidStatement(statement_id=statement_id,
                            plaid_item_id=self.item.item_id,
                            plaid_account_id=account.account_id,
                            period_start=start, period_end=end)
        db.session.add(st)
        stmts.apply_parse(st, stmts.parse_statement(pdf))
        db.session.commit()
        return st

    def _txn(self, account_id, txn_id, amount, when):
        db.session.add(BankTransaction(
            plaid_transaction_id=txn_id, account_id=account_id,
            date=when, amount=amount, name='TXN'))
        db.session.commit()

    def _only(self, account):
        rows = stmts.anchors_for_account(account.account_id)
        self.assertEqual(len(rows), 1)
        return rows[0]

    # ── the two production statements ───────────────────────────────────────

    def test_the_6030_statement_anchors_to_the_banks_own_figures(self):
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-6030-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        stmts.rebuild_statement_anchors()
        a = self._only(self.cash)
        self.assertEqual(a.anchored_opening, 9467.48)
        self.assertEqual(a.anchored_closing, 7793.51)
        self.assertEqual(a.parser_version, stmts.PARSER_VERSION)

    def test_the_9401_statement_anchors_to_the_banks_own_figures(self):
        self._anchor(self.managed, wf_advisors_managed_pdf(), 's-9401-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        stmts.rebuild_statement_anchors()
        a = self._only(self.managed)
        self.assertEqual(a.anchored_opening, 3507.75)
        self.assertEqual(a.anchored_closing, 9702.20)

    def test_the_anchor_takes_cash_not_total_account_value(self):
        """…9401 closed the month at $1,557,537.86 of total value and
        $9,702.20 of cash. Only cash can be reconciled against a transaction
        feed — the mirror has no record of market movement — so anchoring on
        portfolio value would make every brokerage period fail by six
        figures."""
        st = self._anchor(self.managed, wf_advisors_managed_pdf(), 's-9401',
                          date(2025, 6, 1), date(2025, 6, 30))
        stmts.rebuild_statement_anchors()
        a = self._only(self.managed)
        self.assertEqual(a.anchored_closing, 9702.20)
        self.assertEqual(st.portfolio_closing_value, 1557537.86)
        self.assertNotEqual(a.anchored_closing, st.portfolio_closing_value)

    # ── the identity, and the two ways it fails ─────────────────────────────

    def test_a_fully_mirrored_period_has_no_variance(self):
        """opening 9,467.48 + (-1,673.97) = closing 7,793.51 exactly. Plaid saw
        everything the bank did."""
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-6030-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        # Plaid's amount is POSITIVE for money leaving, so this is the outflow.
        self._txn('acct-6030', 't-1', 1673.97, date(2025, 6, 15))
        stmts.rebuild_statement_anchors()
        a = self._only(self.cash)
        self.assertEqual(a.transaction_sum, -1673.97)
        self.assertEqual(a.computed_closing, 7793.51)
        self.assertEqual(a.variance, 0.0)
        self.assertTrue(a.reconciles())

    def test_variance_is_what_the_bank_saw_and_plaid_did_not(self):
        """THE POINT OF THE FEATURE. With nothing mirrored, the whole month's
        movement is unexplained — and it is reported as a finding, not an
        error."""
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-6030-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        stmts.rebuild_statement_anchors()
        a = self._only(self.cash)
        self.assertEqual(a.transaction_sum, 0.0)
        self.assertEqual(a.computed_closing, 9467.48)
        self.assertEqual(a.variance, -1673.97)
        self.assertFalse(a.reconciles())
        self.assertIsNone(a.variance_reason)

    def test_a_missing_statement_breaks_the_chain(self):
        """The OTHER failure, kept distinct from variance because the fix is
        different: no transaction data explains it — a PDF is simply missing,
        and every variance after the gap is measured from the wrong baseline."""
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        # July opens at 9,467.48 rather than June's closing 7,793.51.
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-aug',
                     date(2025, 8, 1), date(2025, 8, 31))
        stmts.rebuild_statement_anchors()
        first, second = stmts.anchors_for_account('acct-6030')
        self.assertFalse(first.chain_gap_from_prior)
        self.assertTrue(second.chain_gap_from_prior)

    def test_a_continuous_chain_reports_no_gap(self):
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        july = self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-jul',
                            date(2025, 7, 1), date(2025, 7, 31))
        july.parsed_metadata = dict(july.parsed_metadata,
                                    cash_opening=7793.51, cash_closing=7000.00)
        db.session.commit()
        stmts.rebuild_statement_anchors()
        self.assertFalse(
            stmts.anchors_for_account('acct-6030')[1].chain_gap_from_prior)

    # ── mechanics ───────────────────────────────────────────────────────────

    def test_rebuilding_is_idempotent(self):
        """Unique on statement_id, so a re-run refreshes rather than appending
        a second version of the same period's truth. That is what makes it safe
        to call after every parser upgrade."""
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        first = stmts.rebuild_statement_anchors()
        again = stmts.rebuild_statement_anchors()
        self.assertEqual(first['written'], again['written'])
        self.assertEqual(len(stmts.anchors_for_account('acct-6030')), 1)

    def test_a_reparse_rebuilds_the_chain_behind_it(self):
        """An anchor built from figures a later recognizer corrected asserts
        the OLD numbers as this account's balance truth — the v0.4.41 lesson,
        one layer up."""
        st = self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-jun',
                          date(2025, 6, 1), date(2025, 6, 30))
        path = stmts.pdf_path_for('item-abc', 'acct-6030', '2025-06', 's-jun')
        stmts.store_pdf(path, wf_advisors_june_2025_pdf())
        st.pdf_path = path
        st.parsed_metadata = dict(st.parsed_metadata, parser_version='0.0.1')
        db.session.commit()
        result = stmts.reparse_stale()
        self.assertGreaterEqual(result['anchors'], 1)
        self.assertEqual(self._only(self.cash).anchored_closing, 7793.51)

    def test_a_statement_with_no_readable_balance_is_skipped(self):
        """An anchor asserting nothing is worse than an absent one — it makes
        the chain look continuous where it isn't."""
        st = PlaidStatement(statement_id='s-blank',
                            plaid_item_id=self.item.item_id,
                            plaid_account_id='acct-6030',
                            period_start=date(2025, 9, 1),
                            period_end=date(2025, 9, 30))
        db.session.add(st)
        db.session.commit()
        result = stmts.rebuild_statement_anchors()
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(stmts.anchors_for_account('acct-6030'), [])

    def test_anchoring_is_company_agnostic(self):
        """No ERPNext Company, no ERPNext mapping — still anchored. The whole
        point is holding history for books that do not exist yet."""
        self.assertIsNone(self.cash.erpnext_bank_account_name)
        self.assertIsNone(self.cash.owning_company)
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        self.assertEqual(stmts.rebuild_statement_anchors()['written'], 1)

    def test_nothing_is_pushed_to_erpnext(self):
        """A property of the release, not an oversight: these accounts move to
        another instance later, and a correction posted now is work to reverse
        then."""
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        with unittest.mock.patch('app.sync_engine.get_erp_client_or_none') as erp:
            stmts.rebuild_statement_anchors()
        erp.assert_not_called()
        self.assertEqual(GeneratedJournalEntry.query.count(), 0)

    def test_the_summary_is_the_headline_number(self):
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        stmts.rebuild_statement_anchors()
        summary = stmts.anchor_summary('acct-6030')
        self.assertEqual(summary['periods'], 1)
        self.assertEqual(summary['variance'], -1673.97)
        self.assertEqual(summary['unexplained'], 1)
        self.assertEqual(summary['gaps'], 0)

    # ── the pages ───────────────────────────────────────────────────────────

    def test_the_reconciliation_page_renders_the_chain(self):
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        stmts.rebuild_statement_anchors()
        resp = self.client_.get('/admin/reconciliation/acct-6030')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        for text in ('9467.48', '7793.51', '-1673.97', 'Anchored opening',
                     'Variance', 'Download CSV'):
            self.assertIn(text, body.replace(',', ''), text)

    def test_the_reconciliation_page_renders_with_no_anchors(self):
        resp = self.client_.get('/admin/reconciliation')
        self.assertEqual(resp.status_code, 200)

    def test_the_csv_carries_the_whole_chain(self):
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        stmts.rebuild_statement_anchors()
        resp = self.client_.get('/admin/reconciliation/acct-6030/csv')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/csv', resp.headers['Content-Type'])
        self.assertIn('no-store', resp.headers['Cache-Control'])
        body = resp.data.decode()
        self.assertIn('anchored_opening', body)
        self.assertIn('9467.48', body)
        self.assertIn('-1673.97', body)

    def test_the_rebuild_endpoint_works(self):
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        resp = self.client_.post('/admin/statements/rebuild_anchors',
                                 data={'account_id': 'acct-6030'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(stmts.anchors_for_account('acct-6030')), 1)

    def test_the_accounts_page_shows_the_variance(self):
        self._anchor(self.cash, wf_advisors_june_2025_pdf(), 's-jun',
                     date(2025, 6, 1), date(2025, 6, 30))
        stmts.rebuild_statement_anchors()
        body = self.client_.get('/admin/accounts').data.decode()
        self.assertIn('unexplained', body)
        self.assertIn('/admin/reconciliation/acct-6030', body)


class AccountPairingTest(StatementsBase):
    """Pairing a WF Advisors brokerage account with its cash-services
    companion (v0.4.44).

    Wells Fargo splits one economic account across two Plaid accounts, and the
    split is invisible until you reconcile: the brokerage side holds the
    statements and the securities activity but ZERO BankTransactions, while the
    companion checking account holds every cash movement and no statements at
    all. Anchoring the brokerage account alone measures the bank's closing
    balance against a feed that is structurally empty, so every period comes
    back unexplained for a reason that has nothing to do with the books."""

    def setUp(self):
        super().setUp()
        self.client_ = self.app.test_client()
        self.brokerage = self._acct('acct-brk', 'BUSINESS BROKERAGE ...6030',
                                    '6030', 'investment', 'brokerage')
        # The fixture prints 'Brokerage Cash Services number: 1234567890',
        # whose last four digits are this account's Plaid mask.
        self.cash = self._acct('acct-cash', 'BUSINESS BROKERAGE CASH ...7890',
                               '7890', 'depository', 'checking')

    def _acct(self, account_id, name, mask, type_, subtype):
        a = PlaidAccount(account_id=account_id, item_id=self.item.item_id,
                         name=name, mask=mask, type=type_, subtype=subtype)
        db.session.add(a)
        db.session.commit()
        return a

    def _statement(self, pdf=None, statement_id='s-jun'):
        st = PlaidStatement(statement_id=statement_id,
                            plaid_item_id=self.item.item_id,
                            plaid_account_id=self.brokerage.account_id,
                            period_start=date(2025, 6, 1),
                            period_end=date(2025, 6, 30))
        db.session.add(st)
        stmts.apply_parse(st, stmts.parse_statement(
            pdf if pdf is not None else wf_advisors_june_2025_pdf()))
        db.session.commit()
        return st

    def _txn(self, account_id, txn_id, amount):
        db.session.add(BankTransaction(
            plaid_transaction_id=txn_id, account_id=account_id,
            date=date(2025, 6, 15), amount=amount, name='TXN'))
        db.session.commit()

    # ── capturing the key ───────────────────────────────────────────────────

    def test_the_statement_names_its_cash_services_account(self):
        st = self._statement()
        self.assertEqual(st.cash_services_account_number, '1234567890')
        self.assertEqual(st.parsed_metadata['cash_services_number'],
                         '1234567890')

    def test_the_managed_fixture_names_a_different_companion(self):
        st = PlaidStatement(statement_id='s-managed',
                            plaid_item_id=self.item.item_id,
                            plaid_account_id=self.brokerage.account_id)
        db.session.add(st)
        stmts.apply_parse(st, stmts.parse_statement(wf_advisors_managed_pdf()))
        db.session.commit()
        self.assertEqual(st.cash_services_account_number, '9876504321')

    def test_the_account_number_is_not_treated_as_a_figure(self):
        """It is an identifier — leading zeros matter and it is not money."""
        m = stmts.parse_statement(wf_advisors_june_2025_pdf())['metadata']
        keys = {k for k, _, _ in stmts.metadata_figures(m)}
        self.assertNotIn('cash_services_number', keys)

    # ── auto-detection ──────────────────────────────────────────────────────

    def test_the_last_four_digits_pair_the_accounts(self):
        self._statement()
        result = stmts.autolink_cash_services()
        self.assertEqual(result['paired'], 1)
        db.session.refresh(self.brokerage)
        self.assertEqual(self.brokerage.paired_account_id, 'acct-cash')

    def test_an_existing_pairing_is_never_overwritten(self):
        """A manual pairing wins — the operator can see things the PDF can't."""
        self.brokerage.paired_account_id = 'acct-manual'
        db.session.commit()
        self._statement()
        result = stmts.autolink_cash_services()
        self.assertEqual(result['paired'], 0)
        self.assertEqual(result['already'], 1)
        self.assertEqual(self.brokerage.paired_account_id, 'acct-manual')

    def test_two_candidates_sharing_a_mask_are_left_unpaired(self):
        """Pairing the WRONG cash account silently folds another account's
        transactions into this reconciliation — worse than a visible gap."""
        self._acct('acct-dupe', 'OTHER CASH ...7890', '7890', 'depository',
                   'checking')
        self._statement()
        result = stmts.autolink_cash_services()
        self.assertEqual(result['paired'], 0)
        self.assertEqual(result['ambiguous'], 1)
        self.assertIsNone(self.brokerage.paired_account_id)

    def test_pairing_never_crosses_a_company(self):
        """Folding one entity's cash into another's reconciliation is never
        right."""
        self.cash.owning_company = 'Some Other Entity, LLC'
        db.session.commit()
        self._statement()
        self.assertEqual(stmts.autolink_cash_services()['paired'], 0)

    def test_the_name_pattern_is_the_fallback_when_no_number_is_printed(self):
        """A name is a convention; the statement is a fact. So this only runs
        when the statement is silent."""
        self.cash.mask = '9999'          # last-4 no longer matches
        db.session.commit()
        st = self._statement()
        st.cash_services_account_number = None
        st.parsed_metadata = {k: v for k, v in st.parsed_metadata.items()
                              if k != 'cash_services_number'}
        db.session.commit()
        self.assertEqual(stmts.autolink_cash_services()['paired'], 1)
        self.assertEqual(self.brokerage.paired_account_id, 'acct-cash')

    def test_detection_is_idempotent(self):
        self._statement()
        stmts.autolink_cash_services()
        self.assertEqual(stmts.autolink_cash_services()['paired'], 0)

    # ── what pairing does to the anchor chain ───────────────────────────────

    def test_an_unpaired_brokerage_account_reports_the_month_unexplained(self):
        """Prior behaviour, preserved: with no pairing the cash feed is empty
        and the whole month is variance."""
        self._statement()
        self._txn('acct-cash', 't-cash', 1673.97)
        stmts.rebuild_statement_anchors()
        anchor = stmts.anchors_for_account('acct-brk')[0]
        self.assertEqual(anchor.transaction_sum, 0.0)
        self.assertEqual(anchor.variance, -1673.97)

    def test_pairing_counts_the_companions_transactions(self):
        """THE POINT. The same transactions were always there — on the other
        account."""
        self._statement()
        self._txn('acct-cash', 't-cash', 1673.97)
        self.brokerage.paired_account_id = 'acct-cash'
        db.session.commit()
        stmts.rebuild_statement_anchors()
        anchor = stmts.anchors_for_account('acct-brk')[0]
        self.assertEqual(anchor.transaction_sum, -1673.97)
        self.assertEqual(anchor.computed_closing, 7793.51)
        self.assertEqual(anchor.variance, 0.0)
        self.assertTrue(anchor.reconciles())

    def test_both_sides_are_summed_not_just_the_companion(self):
        self._statement()
        self._txn('acct-brk', 't-own', 673.97)
        self._txn('acct-cash', 't-cash', 1000.00)
        self.brokerage.paired_account_id = 'acct-cash'
        db.session.commit()
        stmts.rebuild_statement_anchors()
        self.assertEqual(stmts.anchors_for_account('acct-brk')[0]
                         .transaction_sum, -1673.97)

    def test_security_transactions_are_not_double_counted(self):
        """signed_movement already sums them for the brokerage account; adding
        the companion's SecurityTransactions too would replace one wrong answer
        with another."""
        from app.models import SecurityTransaction
        self._statement()
        db.session.add(SecurityTransaction(
            plaid_investment_transaction_id='iv-1', account_id='acct-cash',
            date=date(2025, 6, 15), amount=500.00, type='buy'))
        self._txn('acct-cash', 't-cash', 1673.97)
        self.brokerage.paired_account_id = 'acct-cash'
        db.session.commit()
        stmts.rebuild_statement_anchors()
        self.assertEqual(stmts.anchors_for_account('acct-brk')[0]
                         .transaction_sum, -1673.97)

    def test_a_reparse_pairs_then_rebuilds_in_that_order(self):
        """Ordering is load-bearing: the key is recovered by the re-parse, and
        the sums depend on the pairing being set."""
        st = self._statement()
        path = stmts.pdf_path_for('item-abc', 'acct-brk', '2025-06', 's-jun')
        stmts.store_pdf(path, wf_advisors_june_2025_pdf())
        st.pdf_path = path
        st.parsed_metadata = dict(st.parsed_metadata, parser_version='0.0.1')
        db.session.commit()
        self._txn('acct-cash', 't-cash', 1673.97)
        result = stmts.reparse_stale()
        self.assertEqual(result['paired'], 1)
        self.assertEqual(stmts.anchors_for_account('acct-brk')[0].variance, 0.0)

    # ── the control ─────────────────────────────────────────────────────────

    def test_the_pairing_form_sets_and_clears(self):
        self._statement()
        resp = self.client_.post('/admin/accounts/pair', data={
            'account_id': 'acct-brk', 'paired_account_id': 'acct-cash'})
        self.assertEqual(resp.status_code, 302)
        db.session.refresh(self.brokerage)
        self.assertEqual(self.brokerage.paired_account_id, 'acct-cash')
        self.client_.post('/admin/accounts/pair', data={
            'account_id': 'acct-brk', 'paired_account_id': ''})
        db.session.refresh(self.brokerage)
        self.assertIsNone(self.brokerage.paired_account_id)

    def test_an_account_cannot_be_paired_with_itself(self):
        self.client_.post('/admin/accounts/pair', data={
            'account_id': 'acct-brk', 'paired_account_id': 'acct-brk'})
        db.session.refresh(self.brokerage)
        self.assertIsNone(self.brokerage.paired_account_id)

    def test_the_accounts_page_offers_the_pairing_control(self):
        body = self.client_.get('/admin/accounts').data.decode()
        self.assertIn('/admin/accounts/pair', body)


class SupersedeChainTest(StatementsBase):
    """Re-linked accounts, whose history is SPLIT across two rows (v0.4.45).

    When a bank is re-linked Plaid mints a new account_id for the same physical
    account. Bank Bridge records that with `superseded_by_account_id` — but the
    transactions do not move: everything before the re-link stays on the old
    row, everything after lands on the new one. On the live install ••3158 has
    two rows, one holding 2025-06 → 2026-03 and the other 2026-04 onward, and
    which one hygiene marked 'active' was arbitrary.

    So any query filtering on ONE account_id sees roughly half the history and
    reports the other half as missing money. That is the bug: pairing ••6030 to
    either ••3158 row produced a partner sum of $0 for ten of thirteen periods.
    """

    def setUp(self):
        super().setUp()
        self.client_ = self.app.test_client()
        self.brokerage = self._acct('brk-new', 'BUSINESS BROKERAGE', '6030',
                                    'investment', 'brokerage')
        # The cash side, split by a re-link: `cash-old` was superseded by
        # `cash-new`, and each holds part of the same account's history.
        self.cash_old = self._acct('cash-old', 'BROKERAGE CASH', '3158',
                                   'depository', 'checking')
        self.cash_new = self._acct('cash-new', 'BROKERAGE CASH', '3158',
                                   'depository', 'checking')
        self.cash_old.superseded_by_account_id = 'cash-new'
        db.session.commit()

    def _acct(self, account_id, name, mask, type_, subtype, item_id=None):
        a = PlaidAccount(account_id=account_id,
                         item_id=item_id or self.item.item_id,
                         name=name, mask=mask, type=type_, subtype=subtype)
        db.session.add(a)
        db.session.commit()
        return a

    def _statement(self):
        st = PlaidStatement(statement_id='s-jun',
                            plaid_item_id=self.item.item_id,
                            plaid_account_id='brk-new',
                            period_start=date(2025, 6, 1),
                            period_end=date(2025, 6, 30))
        db.session.add(st)
        stmts.apply_parse(st, stmts.parse_statement(
            wf_advisors_june_2025_pdf()))
        db.session.commit()
        return st

    def _txn(self, account_id, txn_id, amount):
        db.session.add(BankTransaction(
            plaid_transaction_id=txn_id, account_id=account_id,
            date=date(2025, 6, 15), amount=amount, name='TXN'))
        db.session.commit()

    def _pair_and_sum(self, partner_id):
        self.brokerage.paired_account_id = partner_id
        db.session.commit()
        return stmts.anchor_transaction_sum(
            self.brokerage, date(2025, 6, 1), date(2025, 6, 30))

    # ── the walk itself ─────────────────────────────────────────────────────

    def test_the_chain_is_walked_in_both_directions(self):
        """Which row hygiene called 'active' is arbitrary, so asking from
        either end has to give the same answer."""
        self.assertEqual(stmts.supersede_chain('cash-old'),
                         ['cash-new', 'cash-old'])
        self.assertEqual(stmts.supersede_chain('cash-new'),
                         ['cash-new', 'cash-old'])

    def test_the_chain_is_transitive(self):
        """A twice-relinked account is three rows deep."""
        third = self._acct('cash-older', 'BROKERAGE CASH', '3158',
                           'depository', 'checking')
        third.superseded_by_account_id = 'cash-old'
        db.session.commit()
        self.assertEqual(stmts.supersede_chain('cash-new'),
                         ['cash-new', 'cash-old', 'cash-older'])

    def test_an_unlinked_account_is_its_own_chain(self):
        self.assertEqual(stmts.supersede_chain('brk-new'), ['brk-new'])
        self.assertEqual(stmts.supersede_chain(''), [])

    def test_a_cycle_terminates(self):
        """Should be impossible, and therefore will eventually happen."""
        self.cash_new.superseded_by_account_id = 'cash-old'
        db.session.commit()
        self.assertEqual(stmts.supersede_chain('cash-old'),
                         ['cash-new', 'cash-old'])

    # ── the bug, on the partner side ────────────────────────────────────────

    def test_history_split_by_a_relink_is_summed_whole(self):
        """THE LIVE BUG: 243 transactions on one row, 56 on the other."""
        self._statement()
        self._txn('cash-new', 't-new', 673.97)
        self._txn('cash-old', 't-old', 1000.00)
        self.assertEqual(self._pair_and_sum('cash-new'), -1673.97)

    def test_pairing_to_either_row_gives_the_same_answer(self):
        """This is what makes the active/superseded designation cosmetic — Tim
        can re-run hygiene later or not, and reconciliation is unaffected."""
        self._statement()
        self._txn('cash-new', 't-new', 673.97)
        self._txn('cash-old', 't-old', 1000.00)
        self.assertEqual(self._pair_and_sum('cash-new'),
                         self._pair_and_sum('cash-old'))

    def test_the_anchor_variance_collapses_once_the_chain_is_walked(self):
        self._statement()
        self._txn('cash-new', 't-new', 673.97)
        self._txn('cash-old', 't-old', 1000.00)
        self.brokerage.paired_account_id = 'cash-old'
        db.session.commit()
        stmts.rebuild_statement_anchors()
        anchor = stmts.anchors_for_account('brk-new')[0]
        self.assertEqual(anchor.transaction_sum, -1673.97)
        self.assertEqual(anchor.variance, 0.0)
        self.assertTrue(anchor.reconciles())

    # ── the same fix on the brokerage side ──────────────────────────────────

    def test_the_brokerage_side_walks_its_own_chain_too(self):
        """Symmetric on purpose: the brokerage account can have been re-linked
        just as easily, and fixing only the companion leaves the same bug on
        the other half of the identity."""
        from app.models import SecurityTransaction
        old_brk = self._acct('brk-old', 'BUSINESS BROKERAGE', '6030',
                             'investment', 'brokerage')
        old_brk.superseded_by_account_id = 'brk-new'
        db.session.add(SecurityTransaction(
            plaid_investment_transaction_id='iv-old', account_id='brk-old',
            date=date(2025, 6, 10), amount=500.00, type='buy'))
        db.session.commit()
        self._statement()
        self.assertEqual(
            stmts.anchor_transaction_sum(self.brokerage, date(2025, 6, 1),
                                         date(2025, 6, 30)), -500.00)

    def test_a_pairing_into_its_own_chain_is_not_double_counted(self):
        self._statement()
        self._txn('brk-new', 't-own', 1673.97)
        self.assertEqual(self._pair_and_sum('brk-new'), -1673.97)


class ChainDedupeTest(StatementsBase):
    """Re-link OVERLAP: the same real purchase recorded on both account_ids
    (v0.4.46).

    v0.4.45's chain walk fixed history being SPLIT across two rows. It exposed
    the opposite problem in the months either side of the re-link: Plaid
    ingested the same purchases into BOTH account_ids with DIFFERENT
    `plaid_transaction_id`s, so id-based dedupe cannot see it. What matches
    exactly is (date, amount, name).

    On the live install, ••3158's May 2026 window holds 28 rows for 14 real
    purchases, and the anchor summed −$33,775.58 where the bank saw about half
    that. This class reproduces that shape."""

    def setUp(self):
        super().setUp()
        self.brokerage = self._acct('brk', 'BUSINESS BROKERAGE', '6030',
                                    'investment', 'brokerage')
        self.cash_new = self._acct('cash-new', 'BROKERAGE CASH', '3158',
                                   'depository', 'checking')
        self.cash_old = self._acct('cash-old', 'BROKERAGE CASH', '3158',
                                   'depository', 'checking')
        self.cash_old.superseded_by_account_id = 'cash-new'
        self.brokerage.paired_account_id = 'cash-new'
        db.session.commit()

    def _acct(self, account_id, name, mask, type_, subtype):
        a = PlaidAccount(account_id=account_id, item_id=self.item.item_id,
                         name=name, mask=mask, type=type_, subtype=subtype)
        db.session.add(a)
        db.session.commit()
        return a

    def _txn(self, account_id, txn_id, amount, day, name):
        db.session.add(BankTransaction(
            plaid_transaction_id=txn_id, account_id=account_id,
            date=date(2026, 5, day), amount=amount, name=name))

    def _may_sum(self):
        return stmts.anchor_transaction_sum(
            self.brokerage, date(2026, 5, 1), date(2026, 5, 31))

    def test_the_same_purchase_on_both_rows_is_counted_once(self):
        """14 real purchases, 28 rows, different ids on each side — the live
        May 2026 shape. The deduplicated sum equals one side alone."""
        purchases = [(1 + i, 100.00 + i, f'MERCHANT {i}') for i in range(14)]
        for i, (day, amount, name) in enumerate(purchases):
            self._txn('cash-new', f'new-{i}', amount, day, name)
            # Same event, different Plaid id — exactly what a re-link produces.
            self._txn('cash-old', f'old-{i}', amount, day, name)
        db.session.commit()
        one_side = -sum(a for _, a, _ in purchases)
        self.assertEqual(self._may_sum(), round(one_side, 2))

    def test_the_raw_sum_would_have_been_double(self):
        """Pins the bug rather than only the fix."""
        for i in range(14):
            self._txn('cash-new', f'new-{i}', 100.00, 1 + i, f'M{i}')
            self._txn('cash-old', f'old-{i}', 100.00, 1 + i, f'M{i}')
        db.session.commit()
        rows = BankTransaction.query.all()
        self.assertEqual(len(rows), 28)
        self.assertEqual(round(sum(float(t.amount) for t in rows), 2), 2800.00)
        self.assertEqual(self._may_sum(), -1400.00)

    def test_two_real_purchases_on_ONE_account_are_both_kept(self):
        """Buying the same coffee twice in a day is not a data error. Only the
        cross-account mirror is removed."""
        self._txn('cash-new', 'a', 50.00, 4, 'CAFE')
        self._txn('cash-new', 'b', 50.00, 4, 'CAFE')
        db.session.commit()
        self.assertEqual(self._may_sum(), -100.00)

    def test_a_genuine_repeat_survives_alongside_its_mirror(self):
        """Two on one account, one on the other: the real answer is two, not
        one and not three."""
        self._txn('cash-new', 'a', 50.00, 4, 'CAFE')
        self._txn('cash-new', 'b', 50.00, 4, 'CAFE')
        self._txn('cash-old', 'mirror', 50.00, 4, 'CAFE')
        db.session.commit()
        self.assertEqual(self._may_sum(), -100.00)

    def test_casing_and_missing_names_still_match(self):
        self._txn('cash-new', 'a', 11500.00, 27, '  Tim Transfer ')
        self._txn('cash-old', 'b', 11500.00, 27, 'TIM TRANSFER')
        self._txn('cash-new', 'c', 200.00, 4, None)
        self._txn('cash-old', 'd', 200.00, 4, '')
        db.session.commit()
        self.assertEqual(self._may_sum(), -11700.00)

    def test_a_different_amount_or_date_is_a_different_event(self):
        """A cent or a day apart means two purchases, not one recorded twice."""
        self._txn('cash-new', 'a', 100.00, 4, 'SHOP')
        self._txn('cash-old', 'b', 100.01, 4, 'SHOP')
        self._txn('cash-old', 'c', 100.00, 5, 'SHOP')
        db.session.commit()
        self.assertEqual(self._may_sum(), -300.01)

    def test_a_single_account_chain_is_never_deduped(self):
        """No re-link, no overlap — and no risk of collapsing real repeats."""
        self.brokerage.paired_account_id = None
        self.cash_old.superseded_by_account_id = None
        db.session.commit()
        self._txn('brk', 'a', 75.00, 4, 'SAME')
        self._txn('brk', 'b', 75.00, 4, 'SAME')
        db.session.commit()
        self.assertEqual(self._may_sum(), -150.00)

    def test_security_transactions_are_deduped_too(self):
        """Defensive: the live data doesn't show doubled security rows today,
        but a re-link overlap is a property of the re-link, not the table.

        UNPAIRED here, because as of v0.4.48 a paired brokerage skips its
        security transactions entirely (their cash is on the sweep companion).
        The dedup this asserts is on the security chain of a self-directed
        brokerage, which is where security rows are still summed."""
        from app.models import SecurityTransaction
        self.brokerage.paired_account_id = None
        old_brk = self._acct('brk-old', 'BUSINESS BROKERAGE', '6030',
                             'investment', 'brokerage')
        old_brk.superseded_by_account_id = 'brk'
        for account_id, txn_id in (('brk', 'iv-a'), ('brk-old', 'iv-b')):
            db.session.add(SecurityTransaction(
                plaid_investment_transaction_id=txn_id,
                account_id=account_id, date=date(2026, 5, 10),
                amount=500.00, name='DIV XYZ', type='cash',
                subtype='cash/dividend'))
        db.session.commit()
        self.assertEqual(self._may_sum(), -500.00)


class PairCandidateScopeTest(StatementsBase):
    """Which accounts the pairing dropdown offers (v0.4.45).

    The old rule — any depository under the same Company — was far too wide:
    two brokerage accounts under one entity have two different companions, and
    showing all four made pairing to the wrong one easy. A cash-services
    companion is by construction part of the same Plaid Link connection as the
    brokerage account it serves, so the Item is the correct scope."""

    def setUp(self):
        super().setUp()
        self.client_ = self.app.test_client()
        self.other_item = self._item('item-other')
        self.brokerage = self._acct('brk', 'BUSINESS BROKERAGE', '6030',
                                    'investment', 'brokerage')
        self.companion = self._acct('cash', 'BROKERAGE CASH', '3158',
                                    'depository', 'checking')

    def _acct(self, account_id, name, mask, type_, subtype, item_id=None,
              company=None):
        a = PlaidAccount(account_id=account_id,
                         item_id=item_id or self.item.item_id,
                         name=name, mask=mask, type=type_, subtype=subtype,
                         owning_company=company)
        db.session.add(a)
        db.session.commit()
        return a

    def _options(self, account=None):
        return {c.account_id for c in
                stmts.pair_candidates(account or self.brokerage)}

    def test_the_companion_on_the_same_connection_is_offered(self):
        self.assertEqual(self._options(), {'cash'})

    def test_an_account_on_another_plaid_connection_is_not(self):
        """The scope-creep this fix removes: another WF connection's checking
        account is not this brokerage account's companion."""
        self._acct('other-cash', 'OTHER CASH', '9999', 'depository',
                   'checking', item_id='item-other')
        self.assertEqual(self._options(), {'cash'})

    def test_a_superseded_row_is_not_offered(self):
        """Pairing to one still WORKS via the chain walk — this is about not
        inviting it. Tim paired to a superseded row because the UI let him."""
        retired = self._acct('cash-old', 'BROKERAGE CASH', '3158',
                             'depository', 'checking')
        retired.superseded_by_account_id = 'cash'
        db.session.commit()
        self.assertEqual(self._options(), {'cash'})

    def test_another_brokerage_is_not_a_companion(self):
        self._acct('brk2', 'SECOND BROKERAGE', '9401', 'investment',
                   'brokerage')
        self.assertEqual(self._options(), {'cash'})

    def test_a_depository_account_offers_nothing(self):
        """Pairing is a property of the brokerage side — that is where
        `paired_account_id` lives."""
        self.assertEqual(self._options(self.companion), set())

    def test_the_company_constraint_still_applies(self):
        """Same connection, different entity: folding one company's cash into
        another's reconciliation is never right."""
        self.companion.owning_company = 'Another Entity, LLC'
        self.brokerage.owning_company = 'Orchard Example, LLC'
        db.session.commit()
        self.assertEqual(self._options(), set())

    def test_a_candidate_already_paired_elsewhere_is_hidden(self):
        """A cash-services account serves exactly one brokerage account. Once
        ••3158 is ••6030's cash side, offering it on ••9401 invites moving one
        account's whole history into another's reconciliation in one click."""
        second = self._acct('brk2', 'SECOND BROKERAGE', '9401', 'investment',
                            'brokerage')
        other_cash = self._acct('cash2', 'SECOND CASH', '3194', 'depository',
                                'checking')
        self.brokerage.paired_account_id = 'cash'
        db.session.commit()
        self.assertEqual(self._options(second), {'cash2'})
        self.assertEqual(other_cash.account_id, 'cash2')

    def test_the_current_selection_is_always_still_offered(self):
        """Otherwise opening the dropdown on a paired account shows a blank."""
        self.brokerage.paired_account_id = 'cash'
        db.session.commit()
        self.assertIn('cash', self._options())

    def test_the_detector_and_the_ui_share_one_rule(self):
        """The two disagreeing about what a valid pairing is would itself be a
        bug."""
        self.assertIs(stmts._candidate_partners, stmts.pair_candidates)

    def test_the_dropdown_names_its_scope(self):
        body = self.client_.get('/admin/accounts').data.decode()
        self.assertIn('under this Plaid connection', body)


class SandboxVisibilityTest(StatementsBase):
    """Hiding the Plaid Sandbox test accounts (v0.4.44).

    They are not junk — they are the only accounts with a transaction history
    varied enough to exercise the parser and the reconciliation engine — so
    they are hidden, never deleted."""

    SANDBOX = 'Bank Bridge Test'

    def setUp(self):
        super().setUp()
        self.client_ = self.app.test_client()
        self.real = PlaidAccount(
            account_id='acct-real', item_id=self.item.item_id,
            name='BUSINESS BROKERAGE', mask='6030', type='investment',
            subtype='brokerage', owning_company='Orchard Example, LLC')
        self.fake = PlaidAccount(
            account_id='acct-sandbox', item_id=self.item.item_id,
            name='Plaid Checking', mask='0000', type='depository',
            subtype='checking', owning_company=self.SANDBOX)
        db.session.add_all([self.real, self.fake])
        db.session.commit()

    def test_sandbox_accounts_are_hidden_by_default(self):
        """The default changes behaviour on upgrade, deliberately: a sandbox
        row silently inside a reconciliation total is worse than a missing row
        with a toggle that explains it."""
        self.assertFalse(av.include_sandbox())
        visible = {a.account_id for a in av.visible_accounts()}
        self.assertIn('acct-real', visible)
        self.assertNotIn('acct-sandbox', visible)

    def test_the_toggle_brings_them_back(self):
        av.save({'include_sandbox_accounts': True})
        visible = {a.account_id for a in av.visible_accounts()}
        self.assertIn('acct-sandbox', visible)
        av.save({'include_sandbox_accounts': False})
        self.assertNotIn('acct-sandbox',
                         {a.account_id for a in av.visible_accounts()})

    def test_nothing_is_deleted(self):
        av.save({'include_sandbox_accounts': False})
        av.visible_accounts()
        self.assertIsNotNone(
            PlaidAccount.query.filter_by(account_id='acct-sandbox').first())
        self.assertEqual(PlaidAccount.query.count(), 2)

    def test_the_company_that_marks_a_sandbox_account_is_configurable(self):
        av.save({'sandbox_company': 'Some Other Scratch Entity'})
        self.assertIn('acct-sandbox',
                      {a.account_id for a in av.visible_accounts()})
        av.save({'sandbox_company': self.SANDBOX})
        self.assertNotIn('acct-sandbox',
                         {a.account_id for a in av.visible_accounts()})

    def test_an_item_level_company_marks_its_accounts_too(self):
        """Company resolution is per-account override → Item, the same order
        every other feature uses."""
        self.fake.owning_company = None
        self.item.owning_company = self.SANDBOX
        db.session.commit()
        self.assertNotIn('acct-sandbox',
                         {a.account_id for a in av.visible_accounts()})

    def test_the_summary_explains_the_toggle_before_it_is_flipped(self):
        summary = av.summary()
        self.assertEqual(summary['total_sandbox'], 1)
        self.assertEqual(summary['hidden'], 1)
        self.assertFalse(summary['showing'])
        self.assertEqual(summary['company'], self.SANDBOX)

    def test_settings_survive_a_missing_or_broken_file(self):
        """Migrate-on-read: a stale or unreadable data volume still yields
        correct values."""
        path = os.path.join(self._datadir, 'ui_settings.json')
        with open(path, 'w') as fh:
            fh.write('{not json')
        self.assertFalse(av.include_sandbox())
        self.assertEqual(av.sandbox_company(), self.SANDBOX)

    # ── the pages ───────────────────────────────────────────────────────────

    def test_the_accounts_page_hides_them_and_offers_the_toggle(self):
        body = self.client_.get('/admin/accounts').data.decode()
        self.assertNotIn('Plaid Checking', body)
        self.assertIn('/admin/settings/sandbox_visibility', body)
        self.assertIn('sandbox', body.lower())

    def test_the_accounts_page_tags_them_when_shown(self):
        av.save({'include_sandbox_accounts': True})
        body = self.client_.get('/admin/accounts').data.decode()
        self.assertIn('Plaid Checking', body)
        self.assertIn('SANDBOX', body)

    def test_the_toggle_endpoint_flips_it(self):
        resp = self.client_.post('/admin/settings/sandbox_visibility',
                                 data={'include_sandbox_accounts': '1'})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(av.include_sandbox())
        self.client_.post('/admin/settings/sandbox_visibility', data={})
        self.assertFalse(av.include_sandbox())

    def test_other_pages_agree_with_the_filter(self):
        for url in ('/admin/accounts', '/admin/statements',
                    '/admin/transactions', '/admin/holdings',
                    '/admin/investment_transactions', '/admin/reconciliation'):
            resp = self.client_.get(url)
            self.assertEqual(resp.status_code, 200, url)
            self.assertNotIn('Plaid Checking', resp.data.decode(), url)


class PairedSecurityExclusionTest(StatementsBase):
    """A paired brokerage's cash lives entirely on its sweep companion
    (v0.4.48).

    The companion is a "Brokerage Cash Services" account — a cash-sweep ledger
    where every brokerage event (buy, sell, advisory fee, dividend) is recorded
    as an 'Increase/Decrease from Brokerage activity' bank line. So the
    companion's BankTransactions ARE the account's complete cash story, and the
    brokerage's own SecurityTransactions would double-count it.

    v0.4.47 tried to keep income and drop only trades. Verifying against 26
    months of production data disproved that: keeping income left ••9401 at
    −$30,225.86 total variance; the proposed sign-negation only flipped that to
    +$30,225.86; excluding ALL security transactions drove it to exactly $0.00,
    because the fees and dividends sweep to the companion too."""

    def setUp(self):
        super().setUp()
        self.brokerage = self._acct('brk', 'BUSINESS BROKERAGE', '9401',
                                    'investment', 'brokerage')
        self.cash = self._acct('cash', 'BROKERAGE CASH', '3194',
                               'depository', 'checking')

    def _acct(self, account_id, name, mask, type_, subtype):
        a = PlaidAccount(account_id=account_id, item_id=self.item.item_id,
                         name=name, mask=mask, type=type_, subtype=subtype)
        db.session.add(a)
        db.session.commit()
        return a

    def _pair(self):
        self.brokerage.paired_account_id = 'cash'
        db.session.commit()

    def _sec(self, txn_id, amount, type_, subtype='', name='SEC'):
        from app.models import SecurityTransaction
        db.session.add(SecurityTransaction(
            plaid_investment_transaction_id=txn_id,
            account_id='brk', date=date(2026, 2, 10), amount=amount,
            type=type_, subtype=subtype, name=name))
        db.session.commit()

    def _bank(self, account_id, txn_id, amount, name='TXN'):
        db.session.add(BankTransaction(
            plaid_transaction_id=txn_id, account_id=account_id,
            date=date(2026, 2, 10), amount=amount, name=name))
        db.session.commit()

    def _sum(self):
        return stmts.anchor_transaction_sum(
            self.brokerage, date(2026, 2, 1), date(2026, 2, 28))

    def test_the_bug_from_the_field(self):
        """••9401 July 2025: three kept fees summed to −$3,933.26, which showed
        up as exactly that much variance. The cash side is the companion."""
        self._pair()
        self._sec('iv-fee1', -194.74, 'fee', 'fee/interest', 'ADVISORY FEE')
        self._sec('iv-fee2', -3894.71, 'fee', 'fee/interest', 'QUARTERLY FEE')
        self._sec('iv-cr', 156.19, 'fee', 'fee/miscellaneous fee', 'CREDIT')
        # The real cash movement is a single sweep on the companion.
        self._bank('cash', 'bt-sweep', 3933.26, 'Increase from Brokerage activity')
        self.assertEqual(self._sum(), -3933.26)

    def test_every_security_type_is_excluded_when_paired(self):
        """Trades AND income — the companion sweep carries all of it."""
        self._pair()
        for i, (t, st) in enumerate((('buy', 'buy/buy'), ('sell', 'sell/sell'),
                                     ('cash', 'cash/dividend'),
                                     ('fee', 'fee/interest'),
                                     ('transfer', 'transfer/transfer'),
                                     ('cash', 'cash/deposit'))):
            self._sec(f'iv-{i}', -1000.00, t, st)
        self.assertEqual(self._sum(), 0.0)

    def test_only_the_companion_bank_is_counted(self):
        self._pair()
        self._sec('iv-sell', -50000.00, 'sell', 'sell/sell')
        self._sec('iv-div', -10.00, 'cash', 'cash/dividend')
        # The sale proceeds arrive as a sweep on the companion.
        self._bank('cash', 'bt', -50000.00, 'Decrease from Brokerage activity')
        self.assertEqual(self._sum(), 50000.00)

    def test_an_unpaired_brokerage_still_counts_all_its_securities(self):
        """No companion, no sweep — the security feed is the only cash record,
        so v0.4.48 leaves it untouched."""
        self._sec('iv-sell', -50000.00, 'sell', 'sell/sell')
        self._sec('iv-div', -10.00, 'cash', 'cash/dividend')
        self.assertEqual(self._sum(), 50010.00)

    def test_pairing_is_what_flips_the_behaviour(self):
        """The same data reconciles differently paired vs not — the exclusion
        is a property of pairing, not of the transactions."""
        self._sec('iv-div', -10.00, 'cash', 'cash/dividend')
        self.assertEqual(self._sum(), 10.00)     # unpaired: counted
        self._pair()
        self.assertEqual(self._sum(), 0.0)       # paired: on the companion

    def test_the_variance_collapses_on_a_trading_month(self):
        self._pair()
        st = PlaidStatement(statement_id='s-feb',
                            plaid_item_id=self.item.item_id,
                            plaid_account_id='brk',
                            period_start=date(2026, 2, 1),
                            period_end=date(2026, 2, 28))
        db.session.add(st)
        stmts.apply_parse(st, stmts.parse_statement(
            wf_advisors_managed_pdf()))
        # Bank says cash went 3,507.75 -> 9,702.20, i.e. +6,194.45.
        st.parsed_metadata = dict(st.parsed_metadata,
                                  cash_opening=3507.75, cash_closing=9702.20)
        db.session.commit()
        # A busy month of trades on the brokerage side...
        self._sec('iv-sell', -100000.00, 'sell', 'sell/sell')
        self._sec('iv-buy', 90000.00, 'buy', 'buy/buy')
        self._sec('iv-fee', -3894.71, 'fee', 'fee/interest')
        # ...whose net cash effect is a single sweep on the companion.
        self._bank('cash', 'bt-sweep', -6194.45, 'Increase from Brokerage activity')
        stmts.rebuild_statement_anchors()
        anchor = stmts.anchors_for_account('brk')[0]
        self.assertEqual(anchor.transaction_sum, 6194.45)
        self.assertEqual(anchor.variance, 0.0)



class ReconciliationPickerTest(StatementsBase):
    """The account picker on /admin/reconciliation (v0.4.47).

    A Wells Fargo Advisors setup is FOUR Plaid accounts but TWO
    reconciliations: the brokerage accounts hold the statements and therefore
    the anchors, their cash-services companions hold every transaction but no
    statement to measure it against. Offering all four presented four choices
    for two answers, two of which opened an empty page."""

    def setUp(self):
        super().setUp()
        self.client_ = self.app.test_client()
        self.brk = self._acct('brk-6030', 'BUSINESS BROKERAGE', '6030',
                              'investment')
        self.cash = self._acct('cash-3158', 'BROKERAGE CASH', '3158',
                               'depository')
        self.brk.paired_account_id = 'cash-3158'
        db.session.commit()
        self._anchor(self.brk)

    def _acct(self, account_id, name, mask, type_):
        a = PlaidAccount(account_id=account_id, item_id=self.item.item_id,
                         name=name, mask=mask, type=type_,
                         subtype='brokerage' if type_ == 'investment'
                         else 'checking')
        db.session.add(a)
        db.session.commit()
        return a

    def _anchor(self, account):
        st = PlaidStatement(statement_id=f'st-{account.account_id}',
                            plaid_item_id=self.item.item_id,
                            plaid_account_id=account.account_id,
                            period_start=date(2025, 6, 1),
                            period_end=date(2025, 6, 30))
        db.session.add(st)
        stmts.apply_parse(st, stmts.parse_statement(
            wf_advisors_june_2025_pdf()))
        db.session.commit()
        stmts.rebuild_statement_anchors()

    def test_only_accounts_with_anchors_are_offered(self):
        """Two Plaid accounts, one reconciliation."""
        offered = {a.account_id for a in stmts.accounts_with_anchors()}
        self.assertEqual(offered, {'brk-6030'})

    def test_an_unanchored_brokerage_account_is_not_offered(self):
        """Honest: the page's empty state says to rebuild; listing an account
        with nothing behind it does not."""
        self._acct('brk-9401', 'SECOND BROKERAGE', '9401', 'investment')
        self.assertNotIn('brk-9401',
                         {a.account_id for a in stmts.accounts_with_anchors()})

    def test_the_label_names_the_pair(self):
        """The pair changes what the numbers MEAN — this view aggregates both
        Plaid accounts."""
        self.assertEqual(stmts.account_label(self.brk, self.cash),
                         'BUSINESS BROKERAGE ••6030 ⇄ ••3158')
        self.assertEqual(stmts.account_label(self.cash),
                         'BROKERAGE CASH ••3158')

    def test_the_picker_renders_the_paired_label(self):
        body = self.client_.get('/admin/reconciliation').data.decode()
        self.assertIn('••6030 ⇄ ••3158', body)
        self.assertNotIn('value="cash-3158"', body)

    def test_a_deep_link_to_the_cash_side_redirects_to_the_brokerage(self):
        """A bookmark at the companion should land where the reconciliation
        actually is, not on an empty table."""
        resp = self.client_.get('/admin/reconciliation/cash-3158')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/admin/reconciliation/brk-6030', resp.headers['Location'])
        body = self.client_.get(resp.headers['Location']).data.decode()
        self.assertIn('cash side', body)

    def test_the_query_param_form_redirects_too(self):
        resp = self.client_.get('/admin/reconciliation?account_id=cash-3158')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('brk-6030', resp.headers['Location'])

    def test_an_unpaired_account_without_anchors_falls_back_with_a_note(self):
        orphan = self._acct('orphan', 'LONE CHECKING', '7777', 'depository')
        resp = self.client_.get(f'/admin/reconciliation/{orphan.account_id}')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertIn('no statement anchors', body)
        self.assertIn('••6030', body)

    def test_both_url_forms_reach_the_same_account(self):
        """Both are in circulation; neither may break."""
        by_path = self.client_.get('/admin/reconciliation/brk-6030')
        by_query = self.client_.get('/admin/reconciliation?account_id=brk-6030')
        self.assertEqual(by_path.status_code, 200)
        self.assertEqual(by_query.status_code, 200)
        for body in (by_path.data.decode(), by_query.data.decode()):
            self.assertIn('9467.48', body.replace(',', ''))

    def test_an_unpaired_anchored_account_appears_without_an_arrow(self):
        """A brokerage with no companion is still one reconciliation — it just
        isn't an aggregate, and the label shouldn't imply otherwise."""
        solo = self._acct('brk-solo', 'SOLO BROKERAGE', '5555', 'investment')
        self._anchor(solo)
        self.assertEqual(stmts.account_label(solo), 'SOLO BROKERAGE ••5555')
        body = self.client_.get('/admin/reconciliation').data.decode()
        self.assertIn('SOLO BROKERAGE ••5555', body)
        self.assertIn('••6030 ⇄ ••3158', body)

    def test_sandbox_accounts_stay_out_of_the_picker(self):
        """Even with anchors: the sandbox filter runs over the picker list."""
        fake = self._acct('brk-sandbox', 'Plaid IRA', '5555', 'investment')
        fake.owning_company = 'Bank Bridge Test'
        db.session.commit()
        self._anchor(fake)
        self.assertIn('brk-sandbox',
                      {a.account_id for a in stmts.accounts_with_anchors()})
        body = self.client_.get('/admin/reconciliation').data.decode()
        self.assertNotIn('Plaid IRA', body)

    def test_the_accounts_page_points_the_cash_row_at_its_brokerage(self):
        body = self.client_.get('/admin/accounts').data.decode()
        self.assertIn('reconciled with ••6030', body)


class PeriodTagSummaryTest(StatementsBase):
    """The reconciliation view's Reason column, auto-populated from the
    internal tags on a period's transactions (v0.4.49).

    Synthetic tags only — the real attribution vocabulary lives in the
    operator's rules, never in the repo."""

    def setUp(self):
        super().setUp()
        self.client_ = self.app.test_client()
        self.brokerage = self._acct('brk', 'BUSINESS BROKERAGE', '9401',
                                    'investment')
        self.cash = self._acct('cash', 'BROKERAGE CASH', '3194', 'depository')
        self.brokerage.paired_account_id = 'cash'
        db.session.commit()

    def _acct(self, account_id, name, mask, type_):
        a = PlaidAccount(account_id=account_id, item_id=self.item.item_id,
                         name=name, mask=mask, type=type_,
                         subtype='brokerage' if type_ == 'investment'
                         else 'checking')
        db.session.add(a)
        db.session.commit()
        return a

    def _txn(self, account_id, tid, tag, day=15):
        db.session.add(BankTransaction(
            plaid_transaction_id=tid, account_id=account_id,
            date=date(2026, 2, day), amount=-100.0, name='TXN',
            bb_internal_tag=tag))
        db.session.commit()

    def _summary(self):
        return stmts.period_tag_summary(self.brokerage, date(2026, 2, 1),
                                        date(2026, 2, 28))

    def test_one_shared_tag_shows_that_tag(self):
        self._txn('cash', 'a', 'owner_distribution')
        self._txn('cash', 'b', 'owner_distribution', day=16)
        self.assertEqual(self._summary(), 'owner_distribution')

    def test_multiple_tags_show_the_dominant_with_counts(self):
        for i in range(3):
            self._txn('cash', f'o{i}', 'owner_distribution', day=10 + i)
        self._txn('cash', 'f', 'advisory_fee', day=20)
        self.assertEqual(self._summary(),
                         'owner_distribution (3), advisory_fee (1)')

    def test_no_tagged_transactions_yields_empty(self):
        self._txn('cash', 'a', '')
        self.assertEqual(self._summary(), '')

    def test_it_reads_the_companion_chain(self):
        """The tags live on the cash companion — the reason must see them, the
        same set the anchor sum covers."""
        self._txn('cash', 'a', 'internal_sweep')
        self.assertEqual(self._summary(), 'internal_sweep')

    def test_a_re_link_mirror_is_not_double_counted(self):
        """Same fingerprint on both sides of a re-link → one transaction, so
        one tag count."""
        old = self._acct('cash-old', 'BROKERAGE CASH', '3194', 'depository')
        old.superseded_by_account_id = 'cash'
        db.session.commit()
        # identical (date, amount, name) on both rows of the chain
        for aid in ('cash', 'cash-old'):
            db.session.add(BankTransaction(
                plaid_transaction_id=f'{aid}-x', account_id=aid,
                date=date(2026, 2, 15), amount=-100.0, name='SWEEP',
                bb_internal_tag='internal_sweep'))
        db.session.commit()
        self.assertEqual(self._summary(), 'internal_sweep')

    def test_the_reconciliation_view_shows_the_reason(self):
        """End to end: a rule 'descriptor contains OWNER -> owner_distribution',
        backfilled, then rendered in the Reason column."""
        from app.models import CategorizationRule
        db.session.add(CategorizationRule(
            name='owner', priority=100, active=True,
            match_type='description_regex', match_value='OWNER',
            bb_internal_tag='owner_distribution'))
        db.session.add(BankTransaction(
            plaid_transaction_id='ot', account_id='cash',
            date=date(2026, 2, 12), amount=-500.0, name='TEST OWNER DRAW'))
        db.session.commit()
        from app import categorization
        categorization.backfill_internal_tags()
        st = PlaidStatement(statement_id='s-feb',
                            plaid_item_id=self.item.item_id,
                            plaid_account_id='brk',
                            period_start=date(2026, 2, 1),
                            period_end=date(2026, 2, 28))
        db.session.add(st)
        stmts.apply_parse(st, stmts.parse_statement(wf_advisors_managed_pdf()))
        st.parsed_metadata = dict(st.parsed_metadata, cash_opening=3507.75,
                                  cash_closing=9702.20)
        db.session.commit()
        stmts.rebuild_statement_anchors()
        body = self.client_.get('/admin/reconciliation/brk').data.decode()
        self.assertIn('owner_distribution', body)


class FixtureHygieneTest(unittest.TestCase):
    """The fixtures are the one place a real statement's contents could reach
    the repository. This test is the guard.

    A statement PDF carries a home address, a personal email, the advisor's
    name and direct line, the account number and every transaction with
    merchant and card last-4. The text extracted from one carries all of it in
    a form that greps and indexes more cleanly than the PDF does — so a
    committed .txt is worse, not better. Fixtures are hand-written with every
    identifying field replaced; only labels and dollar figures are verbatim."""

    def _fixture_text(self):
        for name in sorted(os.listdir(FIXTURES)):
            with open(os.path.join(FIXTURES, name), encoding='utf-8',
                      errors='replace') as fh:
                yield name, fh.read()

    def test_no_artifact_in_the_fixtures_dir_could_reach_a_commit(self):
        """Asks GIT, not the filesystem.

        The risk being guarded is 'a real statement gets committed', not 'a
        file exists on someone's disk' — and the suite itself drops a stray
        zero-byte `test.pdf` in here on a full run (harmless, ignored, source
        not yet found). Asserting on os.listdir made this test fail for that
        scratch file while a genuinely dangerous file that git happens to
        ignore would pass just the same. Checking what git would actually
        include tests the property that matters and cannot go flaky."""
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        try:
            out = subprocess.run(
                ['git', 'status', '--porcelain', '--untracked-files=all',
                 '--', 'app/tests/fixtures'],
                cwd=root, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover
            self.skipTest(f'git unavailable: {e}')
        if out.returncode != 0:  # pragma: no cover - not a git checkout
            self.skipTest('not a git working tree')
        for line in out.stdout.splitlines():
            path = line[3:].strip().strip('"')
            self.assertTrue(
                path.endswith('_synthetic.txt'),
                f'{path} is visible to git in the fixtures directory. Only '
                f'hand-written *_synthetic.txt fixtures may be committed — a '
                f'PDF or an extracted .txt carries the account holder\'s '
                f'address, email, account number and full transaction detail.')

    def test_fixtures_carry_no_personal_identifiers(self):
        """Patterns, not a blocklist of one person's details — the point is to
        catch the NEXT paste, from whoever's statement it is."""
        patterns = (
            (r'\b\d{3,5}\s+[A-Z][A-Za-z]+\s+(RD|ROAD|ST|STREET|AVE|AVENUE|'
             r'LN|LANE|DR|DRIVE|BLVD|CT|COURT|WAY)\b', 'street address'),
            (r'\b[A-Za-z0-9._%+-]+@(?!example\.com)[A-Za-z0-9.-]+\.'
             r'[A-Za-z]{2,}\b', 'email address'),
            (r'\b\d{5}-\d{4}\b', 'ZIP+4'),
            (r'\b(?!555-000-0000)\d{3}-\d{3}-\d{4}\b', 'phone number'),
            # 0000-0000 is the fully-masked placeholder; any OTHER 4-4 digit
            # group in a fixture is a real account number that got pasted in.
            (r'\b(?!0000-0000)\d{4}-\d{4}\b', 'account number'),
            # v0.4.49 · named people and real merchants the operator's own
            # statements and rules contain. This guard scans ONLY the fixture
            # files, so listing common merchants (Amazon, NAPA…) here is safe —
            # a WF brokerage fixture never mentions them, but a stray paste of
            # real transaction detail would. The tags used in tests are
            # synthetic ('TEST OWNER' → owner_distribution), never these.
            (r'(?i)\b(POLEHN|DANELLA|LENSCRAFTERS|TRADINGVIEW|VZWRLSS|'
             r'TRALOR\s+STATION|SORREN|LES\s+SCHWAB|PRECISION\s+AUTOMOTIVE|'
             r'NAPA|AMAZON|HOME\s+DEPOT)\b',
             'real name or merchant'),
        )
        for name, text in self._fixture_text():
            for pattern, what in patterns:
                found = re.search(pattern, text)
                self.assertIsNone(
                    found, f'{name} looks like it contains a {what}: '
                           f'{found.group(0) if found else ""!r}')

    def test_the_gitignore_refuses_both_artifact_kinds(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        with open(os.path.join(root, '.gitignore'), encoding='utf-8') as fh:
            rules = fh.read()
        for rule in ('app/tests/fixtures/*.pdf',
                     'app/tests/fixtures/*_extracted.txt'):
            self.assertIn(rule, rules)


class DepositAndCardParseTest(StatementsBase):
    """The two layouts the live install holds no real sample of.

    Their field tables were written from the standard Wells Fargo consumer
    layouts and are marked `verified: False` in the metadata for exactly that
    reason — these tests pin the intended behaviour, not a confirmed reading of
    a real document."""

    def test_deposit_activity_summary(self):
        m = stmts.parse_statement(wf_deposit_pdf())['metadata']
        self.assertEqual(m['layout'], 'wf_deposit')
        self.assertFalse(m['verified'])
        self.assertEqual(m['opening_balance'], 17600.00)
        self.assertEqual(m['closing_balance'], 17650.00)
        self.assertEqual(m['deposits_total'], 1200.00)
        self.assertEqual(m['withdrawals_total'], -1138.00)
        self.assertEqual(m['interest_earned'], 0.42)
        self.assertEqual(m['service_fees'], -12.00)
        self.assertEqual(m['average_ledger_balance'], 17_412.88)
        self.assertEqual(m['period_start'], '2026-07-01')
        self.assertEqual(m['period_end'], '2026-07-31')
        self.assertEqual(m['fields_failed'], [])

    def test_credit_card_summary(self):
        m = stmts.parse_statement(wf_card_pdf())['metadata']
        self.assertEqual(m['layout'], 'wf_card')
        self.assertFalse(m['verified'])
        self.assertEqual(m['previous_balance'], 2400.00)
        self.assertEqual(m['new_balance'], 2650.00)
        self.assertEqual(m['payments_total'], -1200.00)
        self.assertEqual(m['purchases_total'], 1425.50)
        self.assertEqual(m['fees_total'], 12.00)
        self.assertEqual(m['interest_charged'], 12.50)
        self.assertEqual(m['minimum_payment_due'], 53.00)
        self.assertEqual(m['credit_limit'], 25000.00)
        self.assertEqual(m['payment_due_date'], '2026-08-22')
        self.assertEqual(m['fields_failed'], [])

    def test_the_headline_balances_come_from_the_layouts_own_vocabulary(self):
        """opening/closing must be filled from whichever field the layout calls
        them — 'previous balance' on a card, 'cash and sweep' on a brokerage —
        so reconciliation and anchoring read one consistent pair."""
        card = stmts.parse_statement(wf_card_pdf())
        self.assertEqual((card['opening'], card['closing']), (2400.00, 2650.00))
        dep = stmts.parse_statement(wf_deposit_pdf())
        self.assertEqual((dep['opening'], dep['closing']), (17600.00, 17650.00))

    def test_depository_vocabulary_still_reads_its_dated_labels(self):
        """The gap rule rejects digits between a label and its amount — but
        'Beginning balance on July 1 $17,600.00' is exactly that, and is the
        wording Wells Fargo's own checking statement uses. The date clause is
        stripped before the rule applies."""
        got = stmts.parse_balances(checking_pdf())
        self.assertEqual(got['opening'], 17600.00)
        self.assertEqual(got['closing'], 17650.00)

    def test_a_brokerage_layout_without_the_summary_yields_nothing(self):
        """Only the sweep table, no summary blocks: the honest answer is None,
        not the first plausible amount on the page."""
        got = stmts.parse_balances(make_pdf([
            'Cash sweep activity',
            'BEGINNING BALANCE TRANSFER FROM BANK DEPOSIT SWEEP06/01 '
            '255,038.26 06/18 -75,594.81',
            'TRANSFER TO BANK DEPOSIT SWEEP ENDING BALANCE06/16 100,000.00 '
            '06/30 39,751.95',
        ]))
        self.assertIsNone(got['opening'])
        self.assertIsNone(got['closing'])


class ParseContinuityTest(StatementsBase):
    """`parse_suspect` — one month's closing balance IS the next month's
    opening, so a break in that chain is evidence the parser (or the set of
    documents) is wrong, available without ERPNext and without a mirrored
    transaction."""

    def _statement(self, label, start, end, opening, closing):
        row = PlaidStatement(statement_id=label,
                             plaid_item_id='item-1',
                             plaid_account_id='acct-1',
                             period_start=start, period_end=end,
                             opening_balance=opening, closing_balance=closing)
        db.session.add(row)
        db.session.commit()
        return row

    def test_a_chained_pair_is_not_suspect(self):
        self._statement('s-jun', date(2026, 6, 1), date(2026, 6, 30),
                        15434.91, 7196.73)
        jul = self._statement('s-jul', date(2026, 7, 1), date(2026, 7, 31),
                              7196.73, 5000.00)
        self.assertFalse(stmts.flag_parse_continuity(jul))

    def test_a_break_in_the_chain_is_flagged(self):
        self._statement('s-jun', date(2026, 6, 1), date(2026, 6, 30),
                        15434.91, 7196.73)
        jul = self._statement('s-jul', date(2026, 7, 1), date(2026, 7, 31),
                              -2289.04, 5000.00)
        self.assertTrue(stmts.flag_parse_continuity(jul))
        self.assertTrue(jul.parse_suspect)

    def test_a_missing_month_is_not_a_suspicion(self):
        """A gap between statements says nothing about either one's figures —
        a whole month of unseen movement sits in between."""
        self._statement('s-jun', date(2026, 6, 1), date(2026, 6, 30),
                        15434.91, 7196.73)
        aug = self._statement('s-aug', date(2026, 8, 1), date(2026, 8, 31),
                              999999.00, 5000.00)
        self.assertFalse(stmts.flag_parse_continuity(aug))

    def test_the_first_statement_ever_held_is_not_suspect(self):
        first = self._statement('s-jun', date(2026, 6, 1), date(2026, 6, 30),
                                15434.91, 7196.73)
        self.assertFalse(stmts.flag_parse_continuity(first))

    def test_an_unparsed_neighbour_yields_no_verdict(self):
        self._statement('s-jun', date(2026, 6, 1), date(2026, 6, 30),
                        15434.91, None)
        jul = self._statement('s-jul', date(2026, 7, 1), date(2026, 7, 31),
                              7196.73, 5000.00)
        self.assertFalse(stmts.flag_parse_continuity(jul))


class ReparseStoredTest(StatementsBase):
    """Re-reading PDFs already on disk — how an install picks up a parser
    improvement, since a pull skips every statement it already holds."""

    def _stored(self, statement_id, label, data, start, end):
        path = stmts.pdf_path_for('item-1', 'acct-1', label, statement_id)
        stmts.store_pdf(path, data)
        row = PlaidStatement(statement_id=statement_id,
                             plaid_item_id='item-1',
                             plaid_account_id='acct-1',
                             period_start=start, period_end=end,
                             pdf_path=path, pdf_bytes=len(data),
                             opening_balance=1.0, closing_balance=2.0)
        db.session.add(row)
        db.session.commit()
        return row

    def test_stale_rows_are_re_read_without_a_download(self):
        row = self._stored('s-jun', '2026-06', wf_advisors_pdf(),
                           date(2026, 6, 1), date(2026, 6, 30))
        stats = stmts.reparse_stored()
        self.assertEqual(stats['examined'], 1)
        self.assertEqual(stats['changed'], 1)
        self.assertEqual(row.opening_balance, 255038.26)
        self.assertEqual(row.closing_balance, 39751.95)
        self.assertEqual(row.portfolio_closing_value, 1313136.16)
        self.assertEqual(row.parse_method, 'wf_advisors')

    def test_a_row_whose_pdf_is_gone_is_left_exactly_as_it_was(self):
        """A statement we can no longer read is not evidence that the figures
        already recorded from it are wrong."""
        row = PlaidStatement(statement_id='s-gone', plaid_item_id='item-1',
                             plaid_account_id='acct-1', pdf_path='',
                             opening_balance=100.0, closing_balance=200.0)
        db.session.add(row)
        db.session.commit()
        stats = stmts.reparse_stored()
        self.assertEqual(stats['unreadable'], 1)
        self.assertEqual(row.opening_balance, 100.0)
        self.assertEqual(row.closing_balance, 200.0)

    def test_continuity_is_judged_on_the_re_parsed_figures(self):
        """The flag pass runs after every row has been re-read — a statement is
        checked against a neighbour that may itself have just changed."""
        self._stored('s-may', '2026-05',
                     wf_advisors_pdf(cash_open='43,962.19',
                                     cash_close='255,038.26'),
                     date(2026, 5, 1), date(2026, 5, 31))
        jun = self._stored('s-jun', '2026-06', wf_advisors_pdf(),
                           date(2026, 6, 1), date(2026, 6, 30))
        stats = stmts.reparse_stored()
        self.assertEqual(stats['suspect'], 0)
        self.assertFalse(jun.parse_suspect)

    def test_a_row_parsed_by_an_older_recognizer_is_stale(self):
        """`parser_version` is what makes a bump self-healing — and the live
        install proved it is needed: v0.4.41 shipped a corrected parser and
        every production row kept its v0.4.40 figures, because a pull skips
        PDFs it already holds and the fix only landed when someone pressed a
        button they didn't know about."""
        row = self._stored('s-jun', '2026-06', wf_advisors_pdf(),
                           date(2026, 6, 1), date(2026, 6, 30))
        self.assertTrue(stmts.is_stale(row))       # never parsed at all
        stmts.reparse_stale()
        self.assertFalse(stmts.is_stale(row))
        self.assertEqual(row.closing_balance, 39751.95)

    def test_reparsing_stale_rows_leaves_current_ones_alone(self):
        """Idempotent and cheap on a settled install: a second pass opens no
        PDF at all."""
        self._stored('s-jun', '2026-06', wf_advisors_pdf(),
                     date(2026, 6, 1), date(2026, 6, 30))
        first = stmts.reparse_stale()
        self.assertEqual(first['examined'], 1)
        self.assertEqual(stmts.reparse_stale()['examined'], 0)
        self.assertEqual(stmts.stale_statements(), [])

    def test_a_row_with_no_pdf_is_never_stale(self):
        """There is nothing to re-read, so its figures are the best obtainable
        — counting it as stale would leave a warning nobody can ever clear."""
        row = PlaidStatement(statement_id='s-gone', plaid_item_id='item-1',
                             plaid_account_id='acct-1', pdf_path='',
                             opening_balance=100.0, closing_balance=200.0)
        db.session.add(row)
        db.session.commit()
        self.assertFalse(stmts.is_stale(row))

    def test_the_whole_metadata_blob_is_rewritten_not_just_the_balances(self):
        row = self._stored('s-jun', '2026-06', wf_advisors_pdf(),
                           date(2026, 6, 1), date(2026, 6, 30))
        stats = stmts.reparse_stored()
        self.assertEqual(row.parsed_metadata['dividends_total'], 1580.25)
        self.assertEqual(row.parsed_metadata['total_gainloss']
                         ['realized_period'], -2928.43)
        self.assertEqual(row.parser_version(), stmts.PARSER_VERSION)
        self.assertGreater(stats['fields'], 20)
        self.assertEqual(stats['failed_fields'], 0)


class ValidationTest(StatementsBase):
    """The three-way comparison — statement vs Plaid vs the mirror.

    This is what makes a statement usable as supporting documentation: not that
    a number was recovered, but that anything disagreeing with it is visible in
    the same row."""

    def setUp(self):
        super().setUp()
        self.account = PlaidAccount(
            account_id='acct-1', item_id=self.item.item_id,
            name='Business Brokerage', mask='6030', type='investment',
            subtype='brokerage', balance_current=39751.95)
        db.session.add(self.account)
        db.session.commit()

    def _statement(self, **kwargs):
        row = PlaidStatement(
            statement_id='s-jun', plaid_item_id=self.item.item_id,
            plaid_account_id='acct-1', period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30), **kwargs)
        db.session.add(row)
        stmts.apply_parse(row, stmts.parse_statement(wf_advisors_pdf()))
        db.session.commit()
        return row

    def _rows(self, verdict):
        return {r['key']: r for r in verdict['rows']}

    def test_plaids_live_balance_is_compared_only_on_the_newest_statement(self):
        """Plaid reports today's balance. Lining it up against a period that
        closed months ago would manufacture a variance meaning nothing."""
        newest = self._statement()
        rows = self._rows(stmts.validate_statement(newest, self.account))
        self.assertEqual(rows['closing_balance']['plaid'], 39751.95)

        older = PlaidStatement(
            statement_id='s-may', plaid_item_id=self.item.item_id,
            plaid_account_id='acct-1', period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31), closing_balance=255038.26)
        db.session.add(older)
        db.session.commit()
        rows = self._rows(stmts.validate_statement(older, self.account))
        self.assertIsNone(rows['closing_balance']['plaid'])

    def test_a_matching_mirror_is_not_flagged(self):
        st = self._statement()
        db.session.add(BankTransaction(
            plaid_transaction_id='t-in', account_id='acct-1',
            date=date(2026, 6, 5), amount=-10000.00, name='deposit'))
        db.session.commit()
        rows = self._rows(stmts.validate_statement(st, self.account))
        self.assertEqual(rows['deposits_total']['statement'], 10000.00)
        self.assertEqual(rows['deposits_total']['computed'], 10000.00)
        self.assertFalse(rows['deposits_total']['flagged'])

    def test_a_mirror_gap_is_flagged_with_its_delta(self):
        """The finding this whole view exists for: the bank says $10,000 came
        in and Bank Bridge mirrored nothing."""
        st = self._statement()
        rows = self._rows(stmts.validate_statement(st, self.account))
        row = rows['deposits_total']
        self.assertEqual(row['statement'], 10000.00)
        self.assertEqual(row['computed'], 0.0)
        self.assertEqual(row['delta'], 10000.00)
        self.assertTrue(row['flagged'])

    def test_a_rounding_cent_on_a_large_balance_is_not_a_finding(self):
        """Flagging needs BOTH thresholds — a penny on $1.3M is not news."""
        st = self._statement()
        st.closing_balance = 39752.45
        db.session.commit()
        rows = self._rows(stmts.validate_statement(st, self.account))
        self.assertFalse(rows['closing_balance']['flagged'])

    def test_plaid_sign_convention_is_normalised_once(self):
        """Plaid's amount is positive when money LEAVES; a statement prints
        withdrawals negative. Both are normalised to cash-in-positive so a
        subtraction between the columns means something."""
        st = self._statement()
        db.session.add(BankTransaction(
            plaid_transaction_id='t-out', account_id='acct-1',
            date=date(2026, 6, 9), amount=20047.16, name='wire out'))
        db.session.commit()
        rows = self._rows(stmts.validate_statement(st, self.account))
        self.assertEqual(rows['withdrawals_total']['statement'], -20047.16)
        self.assertEqual(rows['withdrawals_total']['computed'], -20047.16)
        self.assertFalse(rows['withdrawals_total']['flagged'])

    def test_investment_income_is_matched_against_security_transactions(self):
        from app.models import SecurityTransaction
        st = self._statement()
        db.session.add(SecurityTransaction(
            plaid_investment_transaction_id='iv-1', account_id='acct-1',
            date=date(2026, 6, 30), amount=-1580.25, type='cash',
            subtype='cash/dividend', name='dividend'))
        db.session.commit()
        rows = self._rows(stmts.validate_statement(st, self.account))
        self.assertEqual(rows['dividends_total']['statement'], 1580.25)
        self.assertEqual(rows['dividends_total']['computed'], 1580.25)
        self.assertFalse(rows['dividends_total']['flagged'])

    def test_portfolio_value_has_no_computed_column_and_says_why(self):
        st = self._statement()
        rows = self._rows(stmts.validate_statement(st, self.account))
        row = rows['portfolio_closing']
        self.assertEqual(row['statement'], 1313136.16)
        self.assertIsNone(row['computed'])
        self.assertIn('market movement', row['note'])

    def test_an_unmatched_figure_is_still_shown(self):
        """A statement's assertion is worth surfacing even when nothing here
        can check it — it is what a journal entry cites."""
        st = self._statement()
        rows = self._rows(stmts.validate_statement(st, self.account))
        self.assertEqual(rows['total_gainloss.unrealized']['statement'],
                         44611.96)
        self.assertIsNone(rows['total_gainloss.unrealized']['computed'])

    def test_a_cycle_that_is_not_the_calendar_month_is_surfaced(self):
        st = self._statement()
        self.assertTrue(stmts.validate_statement(st, self.account)
                        ['period_matches'])
        st.period_start = date(2026, 6, 5)
        db.session.commit()
        self.assertFalse(stmts.validate_statement(st, self.account)
                         ['period_matches'])


class StatementPagesRenderTest(StatementsBase):
    """The v0.4.41 additions RENDERED, not just computed.

    A module-level test proves the parser found $1,313,136.16; it says nothing
    about whether the page that shows it to an operator returns 200. This
    codebase has shipped a statements report that 500'd on every request while
    every module test passed (`render_template_string`'s first parameter is
    named `source`, so a context key of that name raised TypeError), so any new
    template context gets a real GET."""

    def setUp(self):
        super().setUp()
        self.client_ = self.app.test_client()
        self.account = PlaidAccount(
            account_id='acct-1', item_id=self.item.item_id,
            name='Business Brokerage', mask='6030',
            type='investment', subtype='brokerage')
        db.session.add(self.account)
        db.session.commit()
        path = stmts.pdf_path_for('item-abc', 'acct-1', '2026-06', 's-jun')
        data = wf_advisors_pdf()
        stmts.store_pdf(path, data)
        self.statement = PlaidStatement(
            statement_id='s-jun', plaid_item_id=self.item.item_id,
            plaid_account_id='acct-1', period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30), pdf_path=path, pdf_bytes=len(data))
        db.session.add(self.statement)
        stmts.apply_parse(self.statement, stmts.parse_balances(data))
        db.session.commit()

    def test_the_statements_page_shows_the_portfolio_value(self):
        resp = self.client_.get('/admin/statements')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertIn('Portfolio value', body)
        self.assertIn('1313136.16', body.replace(',', ''))
        self.assertIn('/admin/statements/reparse', body)

    def test_the_detail_page_renders_the_three_way_validation(self):
        resp = self.client_.get(f'/admin/statements/{self.statement.id}')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertIn('Validation', body)
        self.assertIn('wf_advisors', body)
        self.assertIn('39751.95', body.replace(',', ''))
        # every column header, and figures from across the metadata blob
        for text in ('Statement', 'Plaid', 'Mirror', 'Delta', 'Variance',
                     'Dividends — total', 'Gain/loss — total',
                     'Securities purchased'):
            self.assertIn(text, body, text)

    def test_the_adjust_page_still_renders_and_links_to_the_detail(self):
        resp = self.client_.get(
            f'/admin/statements/{self.statement.id}/adjust')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f'/admin/statements/{self.statement.id}"',
                      resp.data.decode())

    def test_a_statement_with_no_metadata_still_renders(self):
        """A row parsed by an older build has parsed_metadata NULL. The page
        that shows what a statement asserts must not 500 on one that asserts
        nothing."""
        self.statement.parsed_metadata = None
        db.session.commit()
        resp = self.client_.get(f'/admin/statements/{self.statement.id}')
        self.assertEqual(resp.status_code, 200)

    def test_the_reparse_button_works_end_to_end(self):
        self.statement.closing_balance = 0.0
        db.session.commit()
        resp = self.client_.post('/admin/statements/reparse')
        self.assertEqual(resp.status_code, 302)
        db.session.refresh(self.statement)
        self.assertEqual(self.statement.closing_balance, 39751.95)


# ── storage ──────────────────────────────────────────────────────────────────

class StorageTest(StatementsBase):
    def test_pdf_path_layout(self):
        path = stmts.pdf_path_for('item-abc', 'acct-1', '2026-07')
        self.assertEqual(
            path, os.path.join(self._store, 'item-abc', 'acct-1', '2026-07.pdf'))

    def test_path_components_cannot_escape_the_store(self):
        """Plaid's ids are opaque tokens we never chose, so they are treated as
        untrusted input."""
        path = stmts.pdf_path_for('../../etc', 'a/../../b', '../evil')
        self.assertTrue(os.path.realpath(path).startswith(
            os.path.realpath(self._store) + os.sep))
        # No component can name a parent directory: the separators are gone AND
        # the '..' itself is collapsed, so there is nothing left to traverse with.
        relative = os.path.relpath(path, self._store)
        self.assertNotIn('..', relative.split(os.sep))
        self.assertNotIn('..', relative)

    def test_a_reissued_statement_does_not_overwrite_the_first(self):
        """Two statements for one month is rare but real (a corrected
        statement). Silently overwriting would destroy the bank's earlier
        document."""
        first = self._statement(statement_id='st-1', month=7, year=2026)
        second = stmts.pdf_path_for('item-abc', 'acct-1', '2026-07', 'st-2')
        self.assertNotEqual(first.pdf_path, second)

    def test_store_pdf_leaves_no_partial_file(self):
        path = os.path.join(self._store, 'a', 'b', '2026-07.pdf')
        size = stmts.store_pdf(path, b'%PDF-data')
        self.assertEqual(size, 9)
        self.assertEqual(open(path, 'rb').read(), b'%PDF-data')
        self.assertFalse(os.path.exists(path + '.part'))

    def test_resolve_pdf_path_refuses_a_path_outside_the_store(self):
        """pdf_path round-trips through the database, so a file-serving route
        cannot trust it to still be inside the directory it may read from."""
        outside = os.path.join(self._datadir, 'secret.pdf')
        open(outside, 'wb').write(b'%PDF-secret')
        st = self._statement(pdf=False)
        st.pdf_path = outside
        db.session.commit()
        self.assertIsNone(stmts.resolve_pdf_path(st))

    def test_period_bounds(self):
        self.assertEqual(stmts.period_bounds(7, 2026),
                         (date(2026, 7, 1), date(2026, 7, 31)))
        self.assertEqual(stmts.period_bounds(2, 2024),
                         (date(2024, 2, 1), date(2024, 2, 29)))  # leap year
        self.assertEqual(stmts.period_bounds(13, 2026), (None, None))
        self.assertEqual(stmts.period_bounds(None, None), (None, None))


# ── fetching ─────────────────────────────────────────────────────────────────

class FetchTest(StatementsBase):
    def _plaid(self, **kw):
        kw.setdefault('statements', [self._listed()])
        kw.setdefault('statement_pdfs', {'st-1': checking_pdf()})
        return FakePlaidClient(**kw)

    def test_fetch_stores_pdf_and_parses_balances(self):
        self._account()
        stats = stmts.fetch_all(plaid_client=self._plaid())
        self.assertEqual((stats['stored'], stats['parsed']), (1, 1))
        row = PlaidStatement.query.one()
        self.assertEqual(row.statement_id, 'st-1')
        self.assertEqual(row.period_start, date(2026, 7, 1))
        self.assertEqual(row.period_end, date(2026, 7, 31))
        self.assertEqual(row.opening_balance, 17600.00)
        self.assertEqual(row.closing_balance, 17650.00)
        self.assertEqual(row.pdf_path, os.path.join(
            self._store, 'item-abc', 'acct-1', '2026-07.pdf'))
        self.assertTrue(os.path.isfile(row.pdf_path))

    def test_fetch_is_idempotent(self):
        """statement_id is UNIQUE, so a second pull finds the row, sees the PDF
        on disk, and never spends a download."""
        self._account()
        plaid = self._plaid()
        stmts.fetch_all(plaid_client=plaid)
        again = stmts.fetch_all(plaid_client=plaid)
        self.assertEqual(again['skipped_existing'], 1)
        self.assertEqual(again['stored'], 0)
        self.assertEqual(PlaidStatement.query.count(), 1)
        self.assertEqual(
            len([c for c in plaid.calls if c[0] == 'statements_download']), 1)

    def test_missing_pdf_on_disk_is_redownloaded_into_the_same_row(self):
        """A row can outlive its file — a wiped or restored data volume."""
        self._account()
        plaid = self._plaid()
        stmts.fetch_all(plaid_client=plaid)
        row = PlaidStatement.query.one()
        os.remove(row.pdf_path)
        stats = stmts.fetch_all(plaid_client=plaid)
        self.assertEqual(stats['stored'], 1)
        self.assertEqual(PlaidStatement.query.count(), 1)
        self.assertTrue(os.path.isfile(PlaidStatement.query.one().pdf_path))

    def test_download_retries_with_backoff_then_succeeds(self):
        self._account()
        plaid = self._plaid(download_failures=2)
        stats = stmts.fetch_all(plaid_client=plaid)
        self.assertEqual(stats['stored'], 1)
        self.assertEqual(plaid.download_attempts['st-1'], 3)

    def test_backoff_schedule_is_bounded(self):
        self.assertEqual([stmts.backoff_delay(n) for n in (1, 2, 3, 4, 9)],
                         [0.5, 1.0, 2.0, 4.0, 8.0])

    def test_a_statement_that_never_downloads_keeps_its_row(self):
        """The row records that Plaid says this statement exists, so the next
        pull retries it rather than forgetting it."""
        self._account()
        plaid = self._plaid(statement_pdfs={})
        stats = stmts.fetch_all(plaid_client=plaid)
        self.assertEqual((stats['stored'], stats['failed']), (0, 1))
        row = PlaidStatement.query.one()
        self.assertEqual(row.pdf_path, '')
        self.assertFalse(stmts.pdf_exists(row))

    def test_unparseable_pdf_is_still_stored(self):
        """We can't read every bank's layout, but the operator can always open
        the document."""
        self._account()
        plaid = self._plaid(statement_pdfs={'st-1': make_pdf(['MYSTERY BANK'])})
        stats = stmts.fetch_all(plaid_client=plaid)
        self.assertEqual((stats['stored'], stats['parsed']), (1, 0))
        row = PlaidStatement.query.one()
        self.assertTrue(os.path.isfile(row.pdf_path))
        self.assertIsNone(row.opening_balance)

    def test_import_fetch_takes_the_oldest_statement_first(self):
        """The earliest statement carries the earliest opening balance, which is
        what an anchor wants — and a one-click import shouldn't sit through a
        dozen PDF downloads."""
        account = self._account()
        plaid = self._plaid(
            statements=[self._listed('st-jul', month=7),
                        self._listed('st-may', month=5),
                        self._listed('st-jun', month=6)],
            statement_pdfs={'st-jul': checking_pdf(), 'st-may': checking_pdf(),
                            'st-jun': checking_pdf()})
        stats = stmts.fetch_for_account(account, plaid_client=plaid, limit=1)
        self.assertEqual(stats['stored'], 1)
        self.assertEqual(PlaidStatement.query.one().statement_id, 'st-may')

    def test_fetch_restricted_to_one_account(self):
        self._account('acct-1')
        self._account('acct-2', gl='Other - EC')
        plaid = self._plaid(
            statements=[self._listed('st-1', 'acct-1'),
                        self._listed('st-2', 'acct-2')],
            statement_pdfs={'st-1': checking_pdf(), 'st-2': checking_pdf()})
        stmts.fetch_for_account(PlaidAccount.query.filter_by(
            account_id='acct-2').one(), plaid_client=plaid, limit=None)
        self.assertEqual([r.statement_id for r in PlaidStatement.query.all()],
                         ['st-2'])

    def test_disconnected_items_are_skipped(self):
        """A disconnected Item's access_token no longer exists at Plaid."""
        self._account()
        self.item.disconnected = True
        db.session.commit()
        stats = stmts.fetch_all(plaid_client=self._plaid())
        self.assertEqual(stats['listed'], 0)

    def test_disabled_feature_fetches_nothing(self):
        self._account()
        self.app.config['STATEMENTS_ENABLED'] = False
        stats = stmts.fetch_all(plaid_client=self._plaid())
        self.assertEqual(stats['stored'], 0)
        self.assertEqual(PlaidStatement.query.count(), 0)

    def test_unconfigured_plaid_is_a_skip_not_a_crash(self):
        self._account()
        self.assertFalse(plaid_settings.is_configured())
        self.assertEqual(stmts.fetch_all()['stored'], 0)


# ── reconciliation ───────────────────────────────────────────────────────────

class ReconcileTest(StatementsBase):
    def test_reconciles_when_the_mirror_reproduces_the_bank(self):
        """opening 17,600 − 950 of net outflow = 16,650... the statement says
        17,650, so the mirror must show a NET INFLOW of 50 to agree. Plaid's
        amount is positive for money OUT, so that is a sum of -50."""
        account = self._account()
        self._txn('t1', amount=-250.0)          # money in
        self._txn('t2', amount=200.0)           # money out
        st = self._statement(opening=17600.0, closing=17650.0)
        got = stmts.reconcile_statement(st, account)
        self.assertEqual(got['movement'], -50.0)
        self.assertEqual(got['expected_closing'], 17650.0)
        self.assertEqual(got['delta'], 0.0)
        self.assertEqual(got['status'], 'ok')
        self.assertEqual(got['txn_count'], 2)

    def test_flags_a_gap_in_the_mirror(self):
        account = self._account()
        self._txn('t1', amount=-50.0)
        st = self._statement(opening=17600.0, closing=18000.0)
        got = stmts.reconcile_statement(st, account)
        self.assertEqual(got['status'], 'mismatch')
        self.assertEqual(got['delta'], -350.0)   # we are 350 short

    def test_small_differences_are_within_tolerance(self):
        account = self._account()
        st = self._statement(opening=100.0, closing=100.40)
        self.assertEqual(stmts.reconcile_statement(st, account)['status'], 'ok')
        st.closing_balance = 105.0
        db.session.commit()
        self.assertEqual(stmts.reconcile_statement(st, account)['status'],
                         'mismatch')

    def test_credit_card_movement_runs_the_other_way(self):
        """For a card the balance is what you OWE, so a purchase (money out,
        a positive Plaid amount) INCREASES it."""
        account = self._account(type_='credit', subtype='credit card',
                                gl='Chase Card - EC')
        self._txn('t1', amount=250.0)            # a purchase
        st = self._statement(opening=2400.0, closing=2650.0)
        got = stmts.reconcile_statement(st, account)
        self.assertEqual(got['expected_closing'], 2650.0)
        self.assertEqual(got['status'], 'ok')

    def test_pending_and_removed_rows_are_excluded(self):
        """Both are provisional; counting either moves the number by its full
        value and produces a delta that reflects our bookkeeping, not the
        bank's."""
        account = self._account()
        self._txn('t1', amount=-50.0)
        self._txn('t2', amount=9999.0, pending=True)
        self._txn('t3', amount=8888.0, removed=True)
        st = self._statement(opening=17600.0, closing=17650.0)
        self.assertEqual(stmts.reconcile_statement(st, account)['status'], 'ok')

    def test_transactions_outside_the_period_do_not_count(self):
        account = self._account()
        self._txn('t1', amount=-50.0, when=date(2026, 7, 10))
        self._txn('t2', amount=500.0, when=date(2026, 8, 3))
        st = self._statement(opening=17600.0, closing=17650.0)
        self.assertEqual(stmts.reconcile_statement(st, account)['status'], 'ok')

    def test_unparseable_balances_are_no_data_not_a_mismatch(self):
        """An unreadable PDF says nothing about whether the books agree.
        Flagging it as a discrepancy would train an operator to ignore the one
        signal on the page that means something."""
        account = self._account()
        st = self._statement(opening=None, closing=None)
        got = stmts.reconcile_statement(st, account)
        self.assertEqual(got['status'], 'no_data')
        self.assertIsNone(got['delta'])

    def test_computed_fallback_when_pdf_missed_but_mirror_covers_period(self):
        """v0.4.20: an unparseable PDF is not the end of the story if the
        transaction mirror covers the period. The reconciliation still runs on
        opening/closing DERIVED from the mirror, tagged source='computed' so an
        operator sees it is not a bank cross-check.

        With the _account default (balance_current=1000) and one -50 txn
        dated 2026-07-10 inside period [2026-07-01, 2026-07-31]:
          closing (balance at 2026-07-31) = 1000 + Σ(txns after 07-31) = 1000
          opening (balance at 2026-06-30) = 1000 + Σ(txns after 06-30) =
              1000 + (-50) = 950
        Movement for the period is -50 (the one txn inside it). Reconciliation
        expected_closing = apply_movement(opening=950, movement=-50) = 950 -
        (-50) = 1000, which matches the computed closing. Delta is 0 by
        construction — both sides come from the same mirror."""
        account = self._account()
        self._txn('t1', amount=-50.0)
        st = self._statement(opening=None, closing=None)
        got = stmts.reconcile_statement(st, account)
        self.assertEqual(got['status'], 'computed')
        self.assertEqual(got['opening_source'], 'computed')
        self.assertEqual(got['closing_source'], 'computed')
        self.assertEqual(got['opening'], 950.0)
        self.assertEqual(got['closing'], 1000.0)
        self.assertEqual(got['delta'], 0.0)


# ── anchor safety ────────────────────────────────────────────────────────────

class AnchorTest(StatementsBase):
    """choose_anchor_statement is what makes a statement-sourced opening balance
    trustworthy rather than merely automatic."""

    def test_a_reconciling_statement_anchors(self):
        account = self._account()
        self._txn('t1', amount=-50.0)
        self._statement(opening=17600.0, closing=17650.0)
        anchor = stmts.anchor_for(account)
        self.assertIsNotNone(anchor)
        amount, when, statement = anchor
        self.assertEqual(amount, 17600.0)
        self.assertEqual(when, date(2026, 7, 1))
        self.assertEqual(statement.statement_id, 'st-1')

    def test_a_statement_the_mirror_cannot_reproduce_is_refused(self):
        """The strong signal: a gap in the mirror fails exactly this check, so
        a statement that would book a wrong figure is never anchored on."""
        account = self._account()
        self._txn('t1', amount=-50.0)
        self._statement(opening=17600.0, closing=19999.0)
        self.assertIsNone(stmts.anchor_for(account))

    def test_movement_predating_the_anchor_refuses_it(self):
        """Transactions dated before period_start sit underneath an entry that
        already accounts for them — booking the anchor would count them twice."""
        account = self._account()
        self._txn('old', amount=-10.0, when=date(2026, 6, 15))
        self._txn('t1', amount=-50.0, when=date(2026, 7, 10))
        self._statement(opening=17600.0, closing=17650.0)
        self.assertIsNone(stmts.anchor_for(account))

    def test_an_unparseable_statement_never_anchors(self):
        account = self._account()
        self._statement(opening=None, closing=None)
        self.assertIsNone(stmts.anchor_for(account))

    def test_the_oldest_qualifying_statement_wins(self):
        """The earliest safe anchor books the longest stretch of real history."""
        account = self._account()
        self._statement('st-may', month=5, opening=100.0, closing=100.0)
        self._statement('st-jun', month=6, opening=200.0, closing=200.0)
        amount, when, statement = stmts.anchor_for(account)
        self.assertEqual(statement.statement_id, 'st-may')
        self.assertEqual((amount, when), (100.0, date(2026, 5, 1)))

    def test_a_quiet_period_reconciles_trivially(self):
        """No activity means nothing can be missing, so opening == closing is a
        legitimate anchor rather than a suspicious one."""
        account = self._account()
        self._statement(opening=500.0, closing=500.0)
        self.assertEqual(stmts.anchor_for(account)[0], 500.0)

    def test_disabled_feature_never_anchors(self):
        account = self._account()
        self._statement(opening=17600.0, closing=17600.0)
        self.app.config['STATEMENTS_ENABLED'] = False
        self.assertIsNone(stmts.anchor_for(account))


# ── booking an opening balance from a statement ──────────────────────────────

class BookFromStatementTest(StatementsBase):
    def test_prefer_statement_books_the_bank_figure_at_the_period_start(self):
        account = self._account(balance=999.0)
        self._statement(opening=17600.0, closing=17600.0)
        client = self._erp()
        result = obal.book_opening_balance(client, account,
                                          prefer_statement=True)
        self.assertEqual(result['status'], 'booked')
        doc = client.creates_of('Journal Entry')[0][2]
        self.assertEqual(doc['posting_date'], '2026-07-01')
        self.assertEqual(doc['accounts'][0]['debit_in_account_currency'],
                         17600.0)
        self.assertIn('bank statement 2026-07', doc['user_remark'])

    def test_without_prefer_statement_nothing_changes(self):
        """THE UPGRADE GUARANTEE: the import path leaves prefer_statement off,
        so an existing install's opening balances are bit-for-bit what v0.4.4
        booked even once statements are sitting in the database."""
        account = self._account(balance=999.0)
        self._statement(opening=17600.0, closing=17600.0)
        client = self._erp()
        obal.book_opening_balance(client, account)
        doc = client.creates_of('Journal Entry')[0][2]
        self.assertEqual(doc['accounts'][0]['debit_in_account_currency'], 999.0)
        self.assertEqual(doc['posting_date'], date.today().isoformat())
        self.assertIn('at initial link', doc['user_remark'])

    def test_an_explicit_amount_beats_a_statement(self):
        """A caller naming a figure has more context than any heuristic."""
        account = self._account(balance=999.0)
        self._statement(opening=17600.0, closing=17600.0)
        client = self._erp()
        obal.book_opening_balance(client, account, amount=42.0,
                                  prefer_statement=True)
        doc = client.creates_of('Journal Entry')[0][2]
        self.assertEqual(doc['accounts'][0]['debit_in_account_currency'], 42.0)

    def test_falls_back_to_the_plaid_balance_when_no_statement_qualifies(self):
        account = self._account(balance=999.0)
        self._statement(opening=17600.0, closing=19999.0)   # doesn't reconcile
        client = self._erp()
        result = obal.book_opening_balance(client, account,
                                          prefer_statement=True)
        self.assertEqual(result['status'], 'booked')
        doc = client.creates_of('Journal Entry')[0][2]
        self.assertEqual(doc['accounts'][0]['debit_in_account_currency'], 999.0)

    def test_computed_anchor_kicks_in_when_no_statement_qualifies(self):
        """v0.4.21: no bank statement qualifies but the mirror has activity;
        book the transaction-derived opening dated at the earliest mirrored
        transaction rather than fall to plaid_balance dated today.

        balance_current=999, one -50 outflow dated 2026-07-10.
        computed opening (balance at close of 2026-07-09) = 999 + (-50) = 949,
        posted dated 2026-07-10."""
        from app.models import AuditEvent
        account = self._account(balance=999.0)
        self._txn('t1', amount=-50.0, when=date(2026, 7, 10))
        # A statement that doesn't reconcile, so statement_anchor returns None
        # and the computed path is the next fallback.
        self._statement(opening=17600.0, closing=19999.0)
        result = obal.book_opening_balance(self._erp(), account,
                                          prefer_statement=True)
        self.assertEqual(result['status'], 'booked')
        event = AuditEvent.query.filter_by(
            event_type='opening_balance_booked').one()
        payload = event.to_dict()['payload_after']
        self.assertEqual(payload['source'], 'computed')
        self.assertEqual(payload['amount'], 949.0)
        self.assertEqual(payload['posting_date'], '2026-07-10')

    def test_the_audit_event_records_which_source_was_used(self):
        from app.models import AuditEvent
        account = self._account(balance=999.0)
        self._statement(opening=17600.0, closing=17600.0)
        obal.book_opening_balance(self._erp(), account, prefer_statement=True)
        event = AuditEvent.query.filter_by(
            event_type='opening_balance_booked').one()
        self.assertEqual(event.to_dict()['payload_after']['source'], 'statement')
        self.assertEqual(event.to_dict()['payload_after']['statement_id'], 'st-1')


# ── the import path ──────────────────────────────────────────────────────────

class ImportPathTest(StatementsBase):
    def test_import_fetches_a_statement_without_changing_what_it_books(self):
        """Import FETCHES (for the audit trail and /admin/statements) but does
        not anchor: at link time the cached Plaid balance is already the bank's
        own number, so switching would move a balance sheet for nothing."""
        account = self._account(balance=1500.0, gl=None)
        account.erpnext_bank_account_name = None
        db.session.commit()
        plaid = FakePlaidClient(statements=[self._listed()],
                                statement_pdfs={'st-1': checking_pdf()})
        client = self._erp()
        result = erpnext_accounts.import_plaid_account_to_erpnext(
            'acct-1', client=client, plaid_client=plaid)
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['statements']['stored'], 1)
        self.assertEqual(PlaidStatement.query.count(), 1)
        doc = client.creates_of('Journal Entry')[0][2]
        self.assertEqual(doc['accounts'][0]['debit_in_account_currency'], 1500.0)

    def test_import_survives_a_bank_with_no_statements(self):
        account = self._account(balance=1500.0, gl=None)
        account.erpnext_bank_account_name = None
        db.session.commit()
        result = erpnext_accounts.import_plaid_account_to_erpnext(
            'acct-1', client=self._erp(), plaid_client=FakePlaidClient())
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(PlaidStatement.query.count(), 0)

    def test_import_survives_a_statements_api_that_blows_up(self):
        """Best-effort by construction: a Plaid hiccup must degrade to 'no
        statement yet', never unwind an import that already created ERPNext
        records."""
        account = self._account(balance=1500.0, gl=None)
        account.erpnext_bank_account_name = None
        db.session.commit()
        plaid = FakePlaidClient(statements_error=PlaidError('server exploded'))
        result = erpnext_accounts.import_plaid_account_to_erpnext(
            'acct-1', client=self._erp(), plaid_client=plaid)
        self.assertEqual(result['status'], 'imported')


# ── the admin screens ────────────────────────────────────────────────────────

class StatementsUITest(StatementsBase):
    def setUp(self):
        super().setUp()
        self.client = self.app.test_client()

    def test_page_lists_statements_and_flags_a_discrepancy(self):
        self._account()
        self._txn('t1', amount=-50.0)
        self._statement('st-ok', month=5, opening=100.0, closing=100.0)
        self._statement('st-bad', month=7, opening=17600.0, closing=18000.0)
        body = self.client.get('/admin/statements').get_data(as_text=True)
        self.assertIn('2026-07', body)
        self.assertIn('reconciled', body)
        self.assertIn('off by 350.00', body)

    def test_empty_state_explains_what_is_missing(self):
        self._account()
        body = self.client.get('/admin/statements').get_data(as_text=True)
        self.assertIn('No statements yet', body)
        self.assertIn('backfill_statements', body)

    def test_nav_links_the_page(self):
        body = self.client.get('/admin').get_data(as_text=True)
        self.assertIn('/admin/statements', body)

    def test_pdf_is_served_inline(self):
        self._account()
        self._statement()
        resp = self.client.get('/admin/statements/st-1/view')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers['Content-Type'], 'application/pdf')
        self.assertIn('inline', resp.headers['Content-Disposition'])
        self.assertIn('2026-07', resp.headers['Content-Disposition'])
        self.assertEqual(resp.headers['Cache-Control'], 'private, no-store')
        self.assertTrue(resp.get_data().startswith(b'%PDF'))

    def test_unknown_statement_is_a_404(self):
        self.assertEqual(
            self.client.get('/admin/statements/nope/view').status_code, 404)

    def test_a_row_whose_pdf_vanished_is_a_404_not_a_500(self):
        self._account()
        st = self._statement()
        os.remove(st.pdf_path)
        resp = self.client.get('/admin/statements/st-1/view')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('re-download', resp.get_data(as_text=True))

    def test_manual_pull_reports_what_it_did(self):
        self._account()
        with unittest.mock.patch.object(
                stmts, 'fetch_all',
                return_value={**stmts._blank_stats(), 'listed': 3, 'stored': 2,
                              'skipped_existing': 1}):
            resp = self.client.post('/admin/statements/pull')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('stored+2', resp.headers['Location'].replace('%20', '+'))


# ── the scheduled job ────────────────────────────────────────────────────────

class SchedulerTest(StatementsBase):
    def test_interval_defaults_to_monthly(self):
        self.assertEqual(scheduler.statements_interval_or_none(self.app), 30)

    def test_interval_is_configurable_and_disablable(self):
        self.app.config['STATEMENTS_PULL_INTERVAL_DAYS'] = 7
        self.assertEqual(scheduler.statements_interval_or_none(self.app), 7)
        self.app.config['STATEMENTS_PULL_INTERVAL_DAYS'] = 0
        self.assertIsNone(scheduler.statements_interval_or_none(self.app))
        self.app.config['STATEMENTS_PULL_INTERVAL_DAYS'] = 'nonsense'
        self.assertEqual(scheduler.statements_interval_or_none(self.app), 30)

    def test_disabled_feature_adds_no_job(self):
        self.app.config['STATEMENTS_ENABLED'] = False
        self.assertIsNone(scheduler.statements_interval_or_none(self.app))

    def test_the_job_runs_a_pull_and_swallows_failure(self):
        self._account()
        with unittest.mock.patch.object(stmts, 'fetch_all') as fetch, \
                unittest.mock.patch.object(plaid_settings, 'is_configured',
                                           return_value=True):
            fetch.return_value = stmts._blank_stats()
            scheduler._run_statements_pull(self.app)
            fetch.assert_called_once()
            fetch.side_effect = RuntimeError('boom')
            scheduler._run_statements_pull(self.app)   # must not raise


# ── schema ───────────────────────────────────────────────────────────────────

class SchemaTest(StatementsBase):
    def test_the_table_is_created_and_unique_on_statement_id(self):
        from sqlalchemy import inspect
        self.assertIn('plaid_statements', inspect(db.engine).get_table_names())
        self._account()
        self._statement('dup')
        with self.assertRaises(Exception):
            db.session.add(PlaidStatement(statement_id='dup',
                                          plaid_item_id='item-abc',
                                          plaid_account_id='acct-1'))
            db.session.commit()
        db.session.rollback()

    def test_to_dict_never_leaks_a_path_it_does_not_have(self):
        self._account()
        row = self._statement().to_dict()
        self.assertEqual(row['period_label'], '2026-07')
        self.assertEqual(row['opening_balance'], 17600.0)


# ── the backfill script ──────────────────────────────────────────────────────

class BackfillScriptTest(StatementsBase):
    def test_dry_run_lists_without_downloading(self):
        from scripts import backfill_statements
        self._account()
        plaid = FakePlaidClient(statements=[self._listed()],
                                statement_pdfs={'st-1': checking_pdf()})
        with unittest.mock.patch.object(stmts, '_plaid_client_or_none',
                                        return_value=plaid):
            stats = backfill_statements.pull(dry_run=True)
        self.assertEqual(stats['stored'], 1)      # WOULD store
        self.assertEqual(PlaidStatement.query.count(), 0)
        self.assertNotIn('statements_download', [c[0] for c in plaid.calls])

    def test_rebook_replaces_an_estimate_with_a_statement(self):
        from scripts import backfill_statements
        account = self._account(balance=999.0)
        self._statement(opening=17600.0, closing=17600.0)
        client = self._erp()
        obal.book_opening_balance(client, account)     # the v0.4.4 estimate
        self.assertEqual(client.creates_of('Journal Entry')[0][2]
                         ['accounts'][0]['debit_in_account_currency'], 999.0)
        stats = backfill_statements.rebook(client)
        self.assertEqual(len(stats['rebooked']), 1)
        latest = client.creates_of('Journal Entry')[-1][2]
        self.assertEqual(latest['accounts'][0]['debit_in_account_currency'],
                         17600.0)
        self.assertEqual(latest['posting_date'], '2026-07-01')
        self.assertEqual(GeneratedJournalEntry.query.count(), 1)  # re-pointed

    def test_rebook_leaves_an_approved_opening_balance_alone(self):
        """Approving one is a decision, and the pending_review workflow exists
        precisely so nothing overturns it silently."""
        from scripts import backfill_statements
        account = self._account(balance=999.0)
        self._statement(opening=17600.0, closing=17600.0)
        client = self._erp()
        obal.book_opening_balance(client, account)
        entry = obal.existing_entry(account)
        entry.state = 'approved'
        db.session.commit()
        stats = backfill_statements.rebook(client)
        self.assertEqual(stats['rebooked'], [])
        self.assertIn('approved', str(stats['skipped']))

    def test_rebook_skips_an_account_with_no_usable_statement(self):
        from scripts import backfill_statements
        self._account(balance=999.0)
        self._statement(opening=17600.0, closing=19999.0)   # doesn't reconcile
        stats = backfill_statements.rebook(self._erp())
        self.assertEqual(stats['rebooked'], [])
        self.assertIn('no statement reconciles', str(stats['skipped']))


if __name__ == '__main__':
    unittest.main()
