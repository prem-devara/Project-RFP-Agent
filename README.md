# RFP Analysis Agent with MCP Server

A Request for Proposals (RFP) can run past 100 pages, and the requirements are scattered throughout it. Bid teams go through it line by line and build a checklist of everything the proposal has to meet. Missing one item can get a proposal rejected.

The RFP agent builds the checklist by reading the RFP and listing each requirement with:
- A category: Technical, Submission, Evaluation, Legal/Contractual, Insurance, Staffing, or Info.
- The exact wording from the RFP and the page it's on, so anyone can check it against the source.

The bid team then reviews the list instead of starting from a blank page.

## The role of the MCP server, agent and code

- **MCP server:** opens the RFP PDFs, searches them and returns the text page by page (`rfp_server.py`). The agent can't open PDFs by itself. Getting the text page by page ties every requirement to the page it came from, and lets the agent go back to a specific page to check a quote. Because the server is built on MCP, a common standard for connecting AI tools to data, the same RFPs can also be used for Q&A in the Claude desktop app. For example, someone can ask "What insurance does the Kingsport RFP require?" and get an answer with the page it came from.
- **Agent:** reads the RFP through the server, decides what counts as a requirement, assigns a category and writes the checklist (`rfp_agent.py`).
- **Code:** checks the agent's work. It flags any quote not found on its page and any invalid category, and it won't accept the checklist until every page has been read.

The agent follows a rulebook (`RULEBOOK.md`) that defines what counts as a requirement and how to categorise it. I used the same rulebook to build the answer keys.

The checklist is saved in two files:
- **Excel:** one row per requirement, with the requirement, its section, page, category and exact quote. It also has two columns showing whether the quote was found on the page and whether the category is valid.
- **JSON:** the same data, in a format other software can read.

## How I tested it

I wanted to know how many of each RFP's requirements the agent finds, and how often it lists things that aren't requirements.

1. I selected four public RFPs and built the checklist for each by hand, following the rulebook. These are the answer keys.
2. I used one short RFP (Elysian, 3 pages) to build the agent. I kept the other three (Ann Arbor, Kingsport and New York OIG) to test run the RFP agent.
3. I saved the answer keys to the repository before running the agent on the test RFPs. That way the keys couldn't be adjusted after seeing the results.
4. Labelling every page of the long RFPs by hand wasn't practical, so I labelled selected sections completely and scored only those. I chose sections that cover a range of categories.
5. A judge AI model compared the agent's checklist with my answer key, item by item.
6. I ran the RFP agent three times on each test RFP and averaged the results.

## Results

- **Recall:** the percentage of requirements in my answer key that the agent found.
- **Precision:** the percentage of items the agent listed that are real requirements.

| RFP | Pages | Recall | Precision |
|---|---|---|---|
| Ann Arbor | 35 | 95% | 90% |
| Kingsport | 37 | 94% | 94% |
| New York OIG | 112 | 84% | 95% |

## Where it falls short

- **Cover page:** on New York OIG, the agent often missed the deadline for questions, the contract term and the state's targets for minority-, women- and service-disabled veteran-owned businesses.
- **How proposals are scored:** RFPs describe both the scoring criteria and the review process. In Ann Arbor, the criteria are the 4 areas the panel scores, each worth a set number of points, such as professional qualifications. The review process covers how a short-list is chosen, when fee proposals are opened, and when interviews are held. The agent listed several of those process steps as if they were scoring criteria, giving about 9 evaluation items where the key has 4. As a result, only about half of its evaluation items on Ann Arbor were correct. Kingsport and New York OIG showed little or none of this.
- **Missed duties:** on New York OIG, the agent missed part of the support team's ongoing duties in all three runs.
- **Results vary between runs:** on Ann Arbor and Kingsport, the agent finds about the same requirements each time, but the number of extra items changes, so precision can move by up to 7 points. On New York OIG, recall also moved by 10 points between runs.
- **New York OIG's score is less reliable:** I labelled only 9 of its 112 pages, so a few misses on one page move the result noticeably.
- **I built the answer keys myself:** another reviewer might have labelled some items differently.

## What I changed after testing

I fixed the agent's design before testing, and made two changes after seeing results:
- **Response size:** New York OIG's checklist was too long for the agent's response limit, so I raised it. This didn't change how the agent labels requirements.
- **Page coverage:** the first version skipped 55 of New York OIG's 112 pages. I added a check that sends the agent back to any page it hasn't read.

After adding the page-coverage check, I ran the agent four more times on each test RFP: once to confirm the check worked, then three times to get the final results. I used three runs because the agent's output varies from run to run, so the results above are an average.

## Running it

You need Python and an Anthropic API key.

```
python -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env        # add your API key to this file
venv/bin/python rfp_agent.py annarbor     # build the checklist
venv/bin/python score.py annarbor         # compare it with the answer key
```

`eval/README.md` explains how to repeat the full test.
