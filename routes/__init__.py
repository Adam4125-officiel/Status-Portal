"""The portal's routes, split by domain. Each module here registers its views on the
one Flask object, `app.app`, with plain `@app.route` - no blueprint, so no route
address and no endpoint name changed when they moved out of app.py.

app.py imports these modules at its very end, after every helper they use exists, so
a module here can `from app import ...` freely. Two rules keep that safe:

- A name the tests replace on the `app` module (monkeypatch.setattr(app_module, ...),
  or conftest.py reassigning it) must be read as `portal.NAME` at call time, never
  imported with `from app import NAME`: the imported copy would not see the patch.
- Nothing here may register a scheduled task or run anything at import time other
  than defining views: /admin/tasks lists tasks in registration order.
"""
