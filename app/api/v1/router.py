from fastapi import APIRouter

from app.api.v1.endpoints import auth, jobs, projects, users

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(projects.router, prefix="/projects", tags=["projects"])
# Processing-job routes hang off the same /projects prefix (…/{project_id}/process, …/jobs).
api_router.include_router(jobs.router, prefix="/projects", tags=["jobs"])
