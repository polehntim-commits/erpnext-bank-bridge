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
import tempfile
import unittest
import unittest.mock
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, crypto, db  # noqa: E402
from app import erpnext_accounts, erpnext_settings, plaid_settings  # noqa: E402
from app import opening_balance as obal  # noqa: E402
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
            'securities_bought_total': -338826.84,
            'securities_sold_total': 131931.66,
            'income_and_distributions': 1656.03,
            'net_additions_to_cash': 143587.69,
            'net_subtractions_from_cash': -358874.00,
            'other_additions': 10000.00, 'other_subtractions': -20047.16,
            'fees_total': 0.00,
            'interest_total': 980.73, 'sweep_income': 75.78,
            'dividends_ordinary': 434.29, 'dividends_qualified': 1145.96,
            'dividends_total': 1580.25,
            'taxable_income': 2636.76, 'tax_exempt_income': 0.00,
            'total_income': 2636.76,
            'unrealized_gains': 44611.96, 'realized_gains': -2928.43,
            'realized_gains_ytd': -2663.87,
            'gains_short_term_unrealized': -4180.15,
            'gains_long_term_unrealized': 48792.11,
            'available_funds': 39751.95,
        }
        for key, want in expected.items():
            self.assertEqual(m.get(key), want, key)
        self.assertEqual(m['fields_failed'], [])

    def test_the_gain_loss_columns_are_read_by_position(self):
        """The roll-up line is literally 'Total' and carries three columns —
        unrealized, realized this period, realized year to date."""
        m = stmts.parse_statement(wf_advisors_pdf())['metadata']
        self.assertEqual(m['unrealized_gains'], 44611.96)
        self.assertEqual(m['realized_gains'], -2928.43)
        self.assertEqual(m['realized_gains_ytd'], -2663.87)

    def test_a_holdings_table_cannot_answer_for_the_gain_loss_summary(self):
        """~90 holdings rows also begin with 'Total'. Section scoping is what
        stops one of them being read as the account's realized gain."""
        m = stmts.parse_statement(wf_advisors_pdf(with_holdings=True))['metadata']
        self.assertEqual(m['unrealized_gains'], 44611.96)
        self.assertEqual(m['realized_gains'], -2928.43)

    def test_prose_mentioning_a_section_is_not_that_section(self):
        """Page 2 of every WF statement carries 'Income summary: The Income
        summary displays all income as recorded in the tax system…'. Scoping to
        that paragraph would make every income figure wrong."""
        m = stmts.parse_statement(wf_advisors_pdf(with_prose=True))['metadata']
        self.assertEqual(m['total_income'], 2636.76)
        self.assertEqual(m['interest_total'], 980.73)

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

    def test_the_whole_metadata_blob_is_rewritten_not_just_the_balances(self):
        row = self._stored('s-jun', '2026-06', wf_advisors_pdf(),
                           date(2026, 6, 1), date(2026, 6, 30))
        stats = stmts.reparse_stored()
        self.assertEqual(row.parsed_metadata['dividends_total'], 1580.25)
        self.assertEqual(row.parsed_metadata['realized_gains'], -2928.43)
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
        self.assertEqual(rows['unrealized_gains']['statement'], 44611.96)
        self.assertIsNone(rows['unrealized_gains']['computed'])

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
                     'Dividends — total', 'Realized gain/loss — this period',
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
