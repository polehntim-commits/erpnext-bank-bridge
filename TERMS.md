<!-- SPDX-License-Identifier: MIT -->
# Terms of Service

ERPNext Bank Bridge is free, open-source software you run yourself. It is not a
hosted service, and using it does not create an account with, or any ongoing
relationship to, the developer. By running it, you agree to the following.

## As-is, no warranty

The software is provided **"as is"** under the [MIT License](LICENSE), without
warranty of any kind — express or implied — including but not limited to
merchantability, fitness for a particular purpose, and non-infringement. It may
contain bugs. It may miss transactions, misclassify them, or create incorrect
records in ERPNext. You run it at your own risk.

## No support obligation

This is a solo-maintained project offered in good faith. There is **no
guaranteed support, no service-level agreement, and no obligation** to fix bugs,
answer questions, or maintain compatibility with future versions of Plaid or
ERPNext. Bug reports and questions are welcome via GitHub issues, but responses
are best-effort and not promised.

## Your responsibilities

Because you host and operate the software, you are solely responsible for:

- **Your Plaid costs.** You bring your own Plaid account and credentials. Any
  fees Plaid charges for API calls, products, or environments are yours. The app
  includes settings to reduce call volume, but the bill is between you and Plaid.
- **Your backups.** The app does not back up your data. Keeping recoverable
  copies of your database and configuration is up to you.
- **Your compliance.** You are responsible for the legal, tax, and accounting
  correctness of your own books — including whether importing and categorizing
  transactions this way is appropriate for your bookkeeping, and for any filings
  derived from it. This software is a data pipeline, **not** accounting, tax, or
  financial advice.
- **Your security.** Running the app on a network you control, protecting access
  to it, and safeguarding your ERPNext and Plaid credentials are your
  responsibility.

## Limitation of liability

To the maximum extent permitted by law, the developer is **not liable** for any
damages arising from the use of, or inability to use, this software — including
but not limited to accounting errors, missed or duplicated transactions,
incorrect Journal Entries, financial loss, tax consequences, or data loss —
whether or not advised of the possibility of such damages. Verify the records the
app creates against your bank and ERPNext before relying on them.

## Governing terms

Because the software is self-hosted and you are the operator, any dispute is
governed by the laws of **your own jurisdiction**, not the developer's. Nothing
here overrides your rights under the MIT License, which remains the governing
license for the code itself.

## Changes

These terms may change between releases. The version in your copy of the
repository is the one that applies to that copy. Continuing to use a new release
means accepting the terms shipped with it.
