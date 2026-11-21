---
name: documents-to-pdf
description: "Turn notes and Office files into PDFs."
version: 1.0.0
author: Waggle
platforms: [windows]
metadata:
  hermes:
    category: waggle
    tags: [pdf, export, print, document, notes, markdown, obsidian, word, docx, excel, powerpoint, pptx, report, handout, convert]
    related_skills: [research-brief, cad-files]
---

# Documents to PDF

Use for "make this note a PDF", "turn my write-up into a handout", "export this Word file / spreadsheet / slide
deck as a PDF", and for any time you wrote a report and the owner wants a file to print or send.

## Tool

`doc_to_pdf` makes one PDF and returns its path.

- `path` of an Obsidian/Markdown note, .txt, .html: rendered and printed by Chrome in the background. Obsidian
  syntax is handled: frontmatter is dropped, [[wikilinks]] become plain text, embeds become a one-line mention,
  checkboxes and callouts survive.
- `path` of a .docx/.doc/.rtf/.odt, .xlsx/.csv, .pptx: exported by the Microsoft Office installed on this PC
  (hidden). If the owner has Office open, their session is left alone.
- `markdown` (plus `title` and `name`): content you wrote yourself, no source file needed. It lands in Waggle's
  tool-output folder.

The PDF is written next to the source when Waggle may write there (the vault, ~/Claude), else in the tool-output
folder. A name that is taken gets " (2)": nothing is overwritten.

## Steps

1. Pick the source: a vault note (`vault_search` / `note_read` to find it) or write the markdown yourself.
2. Call `doc_to_pdf`. For a long report, give it a clear first `# Heading`: it becomes the title.
3. Tell the owner where the PDF is. If they asked for it on their phone, `send_to_me` with `file` = the returned
   path (PDFs up to ~50 MB).

## Rules

- Wide spreadsheets print across several pages; say so rather than promise a perfect layout.
- Never put secrets, passwords or tokens into a PDF you generate.
- If Office reports a timeout, a dialog is probably waiting on the PC: relay the tool's hint.
