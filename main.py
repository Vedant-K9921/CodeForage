import os
import markdown
from fastapi import FastAPI, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import auth
import database
import git_utils
import models

REPOS_DIR = "./repos"
os.makedirs(REPOS_DIR, exist_ok=True)
models.Base.metadata.create_all(bind=database.engine)  # creates tables on first run

app = FastAPI(title="GitClone")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
get_db = database.get_db


# ---------------- helpers ----------------
def render(request, name, user, **ctx):
    return templates.TemplateResponse(request, name, {"user": user, **ctx})


def error(request, user, message, status=404):
    return templates.TemplateResponse(
        request, "error.html", {"user": user, "message": message}, status_code=status
    )


def load_repo(db, username, repo_name, user):
    """Return (repo, path) or (None, None). Private repos are hidden from other people."""
    owner = db.query(models.User).filter(models.User.username == username).first()
    if not owner or not git_utils.valid_name(repo_name):
        return None, None
    repo = db.query(models.Repository).filter_by(owner_id=owner.id, name=repo_name).first()
    if not repo:
        return None, None
    if repo.is_private and (not user or user.id != owner.id):
        return None, None
    return repo, git_utils.repo_path(REPOS_DIR, username, repo_name)


def pick_branch(path, branch):
    """Use the requested branch if it exists, otherwise main, otherwise the first one."""
    branches = git_utils.get_branches(path)
    if branch in branches:
        return branch, branches
    if "main" in branches:
        return "main", branches
    return (branches[0] if branches else "main"), branches


# ---------------- home / auth ----------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = auth.get_current_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "index.html", None)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return render(request, "register.html", None)


@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if not git_utils.valid_name(username):
        return render(
            request, "register.html", None, error="Username: letters, numbers, . _ - only"
        )
    exists = (
        db.query(models.User)
        .filter((models.User.username == username) | (models.User.email == email))
        .first()
    )
    if exists:
        return render(request, "register.html", None, error="Username or email already taken")
    db.add(
        models.User(
            username=username, email=email, hashed_password=auth.get_password_hash(password)
        )
    )
    db.commit()
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", None)


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.username == username).first()
    if not user or not auth.verify_password(password, user.hashed_password):
        return render(request, "login.html", None, error="Invalid username or password")
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        "access_token", auth.create_access_token(user.username), httponly=True, samesite="lax"
    )
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("access_token")
    return response


# ---------------- dashboard / search / create repo ----------------
# NOTE: fixed paths like /dashboard and /repo/new must come BEFORE /{username}
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = auth.get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    repos = db.query(models.Repository).filter_by(owner_id=user.id).all()
    return render(request, "dashboard.html", user, repos=repos)