# Decisions

**An ADR records an architectural decision: one a principal architect would want to review,
because it is expensive to reverse. It fixes the system's structure, a quality the system is held
to, or a dependency or contract the system is bound to.** Everything else is a commit message.

## What belongs here

A decision earns a file only if it does at least one of these:

- It fixes structure across modules, or across the two halves.
- It fixes a quality the system is held to: tenancy and security, availability, cost, or how
  the system is released and deployed.
- It takes on a dependency or a contract that would take a quarter to swap.

Naming, copy, one module's internals and anything a gate already enforces do not. Their reason
goes in the commit message, and a constraint on how something may be used goes in a docstring or
a test. Under the comment ban a reason always lives outside the code, so that alone never earns a
record. **If unsure, it is a commit message.**

## How

One file per decision: `NNNN-a-sentence-saying-what-was-decided.md`. Yours go in this
directory. `template/` holds the records that came with the template, with numbers of their own.
Cite one of those as `docs/adr/template/NNNN`. `docs/adr/template/0016` says why.

Every record has the same parts:

- `# NNNN. The decision as a claim`, in the present tense. *"Row-level security is on for every
  table, and forced"*, not *"RLS decision"*.
- A `Date:` line.
- `## Status`: accepted or not.
- `## Context`: the forces, and why the obvious design does not work here.
- `## Decision`: the rules. Each rule names the code that holds it: a function, a file, a test.
- `## Considered options`: only the options somebody will propose again, and why not.
- `## Consequences`: what the decision costs, and what it makes impossible.

**Write in Simplified Technical English.** Put one idea in each sentence. Keep most sentences to 20
words or fewer. Use the active voice and the present tense. Use the words in `CONTEXT.md`, and use
one word for one meaning in its bounded context. Do not use idioms or metaphors.

**A record states the decision as it is now.** When the decision changes, change the record. Git
holds the earlier text. Do not add dated amendments, and do not tell the history of the decision.

**A record keeps its number.** A new record takes the number after the highest one in this
directory. When records merge or one goes away, the others keep their numbers, and there is a gap.
Update every citation of a record that merged or went away in the same change: it names the record
that now holds the decision, or the place its rule moved to. So a citation never points at a
different record, and a branch that cites a record still means that record after it merges. If two
branches each add a record, they take the same number, and the later merge takes the next one.

**Cite nothing by section number, and count nothing that lives elsewhere.** Name the thing: the
route, the function, the rule or the invariant. A section number points into one revision of one
document. A count of things that live somewhere else is wrong the first time that number changes.
