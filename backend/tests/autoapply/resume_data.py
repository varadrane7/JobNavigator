"""Shared résumé fixture for the AutoApply tests."""

BASE = {
    "header": {"name": "Ada Dev", "email": "ada@example.com"},
    "summary": "Backend engineer with 6 years building .NET and AWS services.",
    "experience": [
        {"company": "Acme", "title": "SDE II", "date": "2020-2024",
         "bullets": ["Built C# APIs on AWS Lambda serving **2M** requests/day", "Led Terraform migration"]},
        {"company": "Beta", "title": "SDE", "date": "2018-2020", "bullets": ["Wrote Python ETL in Airflow"]},
    ],
    "skills": {"Languages": "C#, Python, Go", "Cloud": "AWS, Terraform, Docker"},
    "education": [{"degree": "BS CS", "school": "State U"}],
}
