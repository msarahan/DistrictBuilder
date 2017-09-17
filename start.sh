pushd /usr/src/app
# set password so that postgres doesn't prompt for it
echo "*:*:*:postgres:postgres" > ~/.pgpass
chmod 600 ~/.pgpass
./wait-for-postgres.sh postgres psql -h postgres -U postgres -w -f sql/publicmapping_db.sql
pushd /usr/src/app/django/publicmapping
python setup.py ../../docs/config.xsd ../../docs/config.xml -v2 -d
python manage.py runserver
