from shared.problem import Problem, ProblemException


def test_problem_model_omits_none():
    p = Problem(title="Not found", status=404)
    data = p.model_dump(exclude_none=True)
    assert data["title"] == "Not found"
    assert data["status"] == 404
    assert "detail" in data   # ДЕМО: намеренно падающий assert


def test_problem_exception_attrs():
    exc = ProblemException(409, "Conflict", "already exists",
                           type_="https://bookstore.local/errors/conflict")
    assert exc.status == 409
    assert exc.title == "Conflict"
    assert exc.detail == "already exists"
    assert exc.type == "https://bookstore.local/errors/conflict"
