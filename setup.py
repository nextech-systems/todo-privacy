from setuptools import find_packages, setup

from todo_privacy import __version__ as version

setup(
	name="todo_privacy",
	version=version,
	description="Keeps ERPNext ToDos private to the people who created, assigned or hold them",
	author='Nexxttech Solutions Ja',
	author_email='nexxttechja@gmail.com',
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	# frappe is already installed in the bench environment; declaring it here
	# would make pip try to resolve it from PyPI.
	install_requires=[],
)
