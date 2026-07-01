## Links and Tags

This section contains [[Foo]] and [[Bar|display text]] inline.

We also have a #tag1 and a #nested/tag within the prose.

Here is a code block that should be ignored:

```
def fake():
    # [[NotALink]] should not be extracted
    pass  # #nottag should also be ignored
```

The final paragraph with #tag1 again (dedup) and a reference to [[Foo]] again (dedup).
