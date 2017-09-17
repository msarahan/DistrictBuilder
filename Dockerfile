FROM python:2.7

RUN mkdir -p /usr/src/app
WORKDIR /usr/src/app

RUN apt-key adv --keyserver ha.pool.sks-keyservers.net --recv-keys B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8

RUN echo 'deb http://apt.postgresql.org/pub/repos/apt/ jessie-pgdg main' ${PG_MAJOR} > /etc/apt/sources.list.d/pgdg.list

RUN apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin \
        gettext \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /usr/src/app/
RUN pip install --no-cache-dir \
        numpy==$(grep "numpy" requirements.txt | cut -d= -f3) \
        scipy==$(grep "scipy" requirements.txt | cut -d= -f3) \
    && pip install --no-cache-dir -r requirements.txt

COPY . /usr/src/app

RUN cat docs/config.dist.xml | sed -e 's|<Database name="YOUR-DATABASE-NAME" user="publicmapping" password="YOUR-DATABASE-PASSWORD" host="OPTIONAL"/>|<Database name="postgres" user="postgres" password="postgres" host="postgres"/>|' > docs/config.xml

ENTRYPOINT ["bash"]
CMD ["start.sh"]