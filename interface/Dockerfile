# Image de l'interface : la page du terminal, servie par nginx, qui fait
# aussi proxy vers l'API en y ajoutant la cle.
#
# Variante "unprivileged" : nginx tourne sous un utilisateur sans droits
# (uid 101) et ecoute sur 8080. Si une faille permettait d'executer du code
# dans le conteneur, elle n'aurait pas les droits root.
FROM nginxinc/nginx-unprivileged:1.30-alpine

# Au demarrage, l'image remplace les ${VARIABLES} des fichiers .template et
# ecrit le resultat dans /etc/nginx/conf.d/. Le filtre limite ce
# remplacement a NOS variables : $host ou $remote_addr restent a nginx.
ENV NGINX_ENVSUBST_FILTER="^CRYPTOBOT_" \
    CRYPTOBOT_API_HOTE=api

COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY snippets/proxy_api.conf /etc/nginx/templates/snippets/proxy_api.conf.template
COPY snippets/securite.conf /etc/nginx/templates/snippets/securite.conf.template
COPY snippets/entetes_communs.conf /etc/nginx/templates/snippets/entetes_communs.conf.template

COPY index.html /usr/share/nginx/html/index.html
COPY static/ /usr/share/nginx/html/static/

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/sante || exit 1
